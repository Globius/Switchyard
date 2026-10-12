// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

// Package enroll trades a one-time (or renewal) token for the agent's server certificate.
//
// The agent generates its own key, so the private key never leaves the VM. The controller signs a
// certificate for the names it has on record for this engine and returns it with the CA, the
// public key that verifies configuration bundles, and a rotating renewal token. Files are written
// atomically and only after the whole exchange has succeeded.
package enroll

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// File names under the agent directory.
const (
	CertFile       = "tls.crt"
	KeyFile        = "tls.key"
	CAFile         = "ca.crt"
	BundleKeyFile  = "bundle.pub"
	RenewTokenFile = "renew.token"
)

// Config says where to enroll and where to put the results.
type Config struct {
	ControllerURL string       // for example https://switchyard.example.test
	Token         string       // enrollment or renewal token
	Dir           string       // usually /etc/switchyard/agent
	Hostname      string       // used as the CSR common name (the controller sets the real names)
	HTTP          *http.Client // nil means a client with a 30 second timeout
	// AllowPlainHTTP permits an http:// controller URL. Tests only: the response carries the CA,
	// the bundle signing key and the renewal token, so over plain HTTP anyone on the path could
	// take over the agent's trust.
	AllowPlainHTTP bool
}

type request struct {
	Token string `json:"token"`
	CSR   string `json:"csr"`
}

type response struct {
	Engine          string `json:"engine"`
	Certificate     string `json:"certificate"`
	CA              string `json:"ca"`
	BundlePublicKey string `json:"bundle_public_key"`
	RenewalToken    string `json:"renewal_token"`
	NotAfter        string `json:"not_after"`
}

// Run performs the exchange and writes the files. It returns the engine name the controller used.
func Run(ctx context.Context, cfg Config) (string, error) {
	if cfg.Token == "" || cfg.ControllerURL == "" || cfg.Dir == "" {
		return "", errors.New("controller URL, token and directory are required")
	}
	if !cfg.AllowPlainHTTP && !strings.HasPrefix(strings.ToLower(cfg.ControllerURL), "https://") {
		return "", errors.New("the controller URL must use https")
	}
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return "", err
	}
	csrDER, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{
		Subject: pkix.Name{CommonName: cfg.Hostname},
	}, key)
	if err != nil {
		return "", err
	}
	csrPEM := pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: csrDER})

	body, _ := json.Marshal(request{Token: cfg.Token, CSR: string(csrPEM)})
	url := strings.TrimRight(cfg.ControllerURL, "/") + "/api/v1/enroll"
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")
	client := cfg.HTTP
	if client == nil {
		client = &http.Client{Timeout: 30 * time.Second}
	}
	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("contacting the controller: %w", err)
	}
	defer resp.Body.Close()
	raw, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("the controller refused enrollment (HTTP %d): %s", resp.StatusCode, firstLine(raw))
	}
	var out response
	if err := json.Unmarshal(raw, &out); err != nil {
		return "", fmt.Errorf("unreadable controller response: %w", err)
	}
	keyDER, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return "", err
	}
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER})
	// The certificate must belong to our key before anything is replaced.
	if _, err := tls.X509KeyPair([]byte(out.Certificate), keyPEM); err != nil {
		return "", fmt.Errorf("the controller returned a certificate that does not match our key: %w", err)
	}
	if out.CA == "" || out.BundlePublicKey == "" || out.RenewalToken == "" {
		return "", errors.New("the controller response is incomplete")
	}
	files := []struct {
		name string
		data string
		mode os.FileMode
	}{
		{KeyFile, string(keyPEM), 0o600},
		{CertFile, out.Certificate, 0o644},
		{CAFile, out.CA, 0o644},
		{BundleKeyFile, out.BundlePublicKey + "\n", 0o644},
		{RenewTokenFile, out.RenewalToken + "\n", 0o600},
	}
	if err := os.MkdirAll(cfg.Dir, 0o750); err != nil {
		return "", err
	}
	for _, f := range files {
		if err := writeAtomic(filepath.Join(cfg.Dir, f.name), []byte(f.data), f.mode); err != nil {
			return "", err
		}
	}
	return out.Engine, nil
}

func firstLine(b []byte) string {
	s := strings.TrimSpace(string(b))
	if i := strings.IndexByte(s, '\n'); i >= 0 {
		s = s[:i]
	}
	if len(s) > 200 {
		s = s[:200]
	}
	return s
}

func writeAtomic(path string, data []byte, mode os.FileMode) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), ".enroll-*")
	if err != nil {
		return err
	}
	name := tmp.Name()
	defer os.Remove(name)
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(name, mode); err != nil {
		return err
	}
	return os.Rename(name, path)
}

// Reloader serves the certificate on disk, re-reading it when the files change, so renewing needs
// no restart.
type Reloader struct {
	CertPath, KeyPath string

	mu      sync.Mutex
	cert    *tls.Certificate
	certMod time.Time
	keyMod  time.Time
}

// GetCertificate implements tls.Config.GetCertificate.
func (r *Reloader) GetCertificate(*tls.ClientHelloInfo) (*tls.Certificate, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	cs, err := os.Stat(r.CertPath)
	if err != nil {
		return nil, err
	}
	ks, err := os.Stat(r.KeyPath)
	if err != nil {
		return nil, err
	}
	if r.cert == nil || !cs.ModTime().Equal(r.certMod) || !ks.ModTime().Equal(r.keyMod) {
		cert, err := tls.LoadX509KeyPair(r.CertPath, r.KeyPath)
		if err != nil {
			if r.cert != nil {
				return r.cert, nil // keep serving the old one while files are being replaced
			}
			return nil, err
		}
		r.cert, r.certMod, r.keyMod = &cert, cs.ModTime(), ks.ModTime()
	}
	return r.cert, nil
}
