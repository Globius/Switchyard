package enroll

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"math/big"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// signer plays the controller: it signs whatever CSR it receives.
type signer struct {
	ca    *x509.Certificate
	key   *ecdsa.PrivateKey
	caPEM string
}

func newSigner(t *testing.T) *signer {
	t.Helper()
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	tmpl := &x509.Certificate{
		SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: "test ca"},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(24 * time.Hour),
		IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign,
	}
	der, err := x509.CreateCertificate(rand.Reader, tmpl, tmpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	ca, _ := x509.ParseCertificate(der)
	return &signer{ca: ca, key: key, caPEM: string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}))}
}

func (s *signer) handler(t *testing.T, wantToken string, mismatch bool) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		var in request
		_ = json.NewDecoder(r.Body).Decode(&in)
		if r.URL.Path != "/api/v1/enroll" || in.Token != wantToken {
			http.Error(w, "invalid or expired token", http.StatusUnauthorized)
			return
		}
		block, _ := pem.Decode([]byte(in.CSR))
		csr, err := x509.ParseCertificateRequest(block.Bytes)
		if err != nil || csr.CheckSignature() != nil {
			t.Errorf("bad CSR: %v", err)
		}
		pub := csr.PublicKey
		if mismatch {
			other, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
			pub = &other.PublicKey
		}
		tmpl := &x509.Certificate{
			SerialNumber: big.NewInt(7), Subject: pkix.Name{CommonName: "se-a"},
			NotBefore: time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour),
		}
		der, err := x509.CreateCertificate(rand.Reader, tmpl, s.ca, pub, s.key)
		if err != nil {
			t.Fatal(err)
		}
		_ = json.NewEncoder(w).Encode(response{
			Engine:          "se-a",
			Certificate:     string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})),
			CA:              s.caPEM,
			BundlePublicKey: "QUJD",
			RenewalToken:    "syren_next",
		})
	}
}

func TestEnrollWritesAllFilesWithSafeModes(t *testing.T) {
	s := newSigner(t)
	srv := httptest.NewServer(s.handler(t, "syenr_ok", false))
	defer srv.Close()
	dir := filepath.Join(t.TempDir(), "agent")
	engine, err := Run(context.Background(), Config{ControllerURL: srv.URL, Token: "syenr_ok", Dir: dir, Hostname: "se-a", AllowPlainHTTP: true})
	if err != nil || engine != "se-a" {
		t.Fatalf("enroll: %q %v", engine, err)
	}
	if _, err := tls.LoadX509KeyPair(filepath.Join(dir, CertFile), filepath.Join(dir, KeyFile)); err != nil {
		t.Errorf("key and certificate do not pair: %v", err)
	}
	if runtime.GOOS != "windows" {
		for name, want := range map[string]os.FileMode{KeyFile: 0o600, RenewTokenFile: 0o600, CertFile: 0o644} {
			info, _ := os.Stat(filepath.Join(dir, name))
			if info.Mode().Perm() != want {
				t.Errorf("%s mode %v, want %v", name, info.Mode().Perm(), want)
			}
		}
	}
	renew, _ := os.ReadFile(filepath.Join(dir, RenewTokenFile))
	if strings.TrimSpace(string(renew)) != "syren_next" {
		t.Errorf("renew token = %q", renew)
	}
}

func TestEnrollRefusedAndMismatchedCertificatesChangeNothing(t *testing.T) {
	s := newSigner(t)
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, KeyFile), []byte("old-key"), 0o600); err != nil {
		t.Fatal(err)
	}
	bad := httptest.NewServer(s.handler(t, "right", false))
	defer bad.Close()
	if _, err := Run(context.Background(), Config{ControllerURL: bad.URL, Token: "wrong", Dir: dir, AllowPlainHTTP: true}); err == nil || !strings.Contains(err.Error(), "401") {
		t.Errorf("a refused token must fail with the status: %v", err)
	}
	mismatch := httptest.NewServer(s.handler(t, "t", true))
	defer mismatch.Close()
	if _, err := Run(context.Background(), Config{ControllerURL: mismatch.URL, Token: "t", Dir: dir, AllowPlainHTTP: true}); err == nil || !strings.Contains(err.Error(), "does not match") {
		t.Errorf("a certificate for another key must be rejected: %v", err)
	}
	if got, _ := os.ReadFile(filepath.Join(dir, KeyFile)); string(got) != "old-key" {
		t.Error("existing files must be untouched after a failed enrollment")
	}
	if _, err := Run(context.Background(), Config{}); err == nil {
		t.Error("missing configuration must be an error")
	}
}

func TestReloaderPicksUpRenewedCertificates(t *testing.T) {
	s := newSigner(t)
	srv := httptest.NewServer(s.handler(t, "t", false))
	defer srv.Close()
	dir := t.TempDir()
	cfg := Config{ControllerURL: srv.URL, Token: "t", Dir: dir, AllowPlainHTTP: true}
	if _, err := Run(context.Background(), cfg); err != nil {
		t.Fatal(err)
	}
	r := &Reloader{CertPath: filepath.Join(dir, CertFile), KeyPath: filepath.Join(dir, KeyFile)}
	first, err := r.GetCertificate(nil)
	if err != nil {
		t.Fatal(err)
	}
	again, _ := r.GetCertificate(nil)
	if first != again {
		t.Error("an unchanged certificate must be served from the cache")
	}
	time.Sleep(20 * time.Millisecond)
	if _, err := Run(context.Background(), cfg); err != nil {
		t.Fatal(err)
	}
	later := time.Now().Add(time.Second)
	_ = os.Chtimes(filepath.Join(dir, CertFile), later, later)
	renewed, err := r.GetCertificate(nil)
	if err != nil || renewed == first {
		t.Errorf("a replaced certificate must be reloaded (err=%v)", err)
	}
}

func TestEnrollmentRefusesAPlainHTTPController(t *testing.T) {
	s := newSigner(t)
	srv := httptest.NewServer(s.handler(t, "t", false))
	defer srv.Close()
	_, err := Run(context.Background(), Config{ControllerURL: srv.URL, Token: "t", Dir: t.TempDir()})
	if err == nil || !strings.Contains(err.Error(), "https") {
		t.Fatalf("an http:// controller URL was accepted: %v", err)
	}
}
