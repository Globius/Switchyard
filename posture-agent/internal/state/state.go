// Package state keeps the device agent's identity and credentials in one directory.
//
// identity.json holds the controller address, the device id and the Ed25519 seed that signs
// reports; tls.key is the key of the client certificate (never sent anywhere); tls.crt and ca.crt
// are what the controller last issued. Everything is written atomically and the secrets 0600.
package state

import (
	"crypto/ecdsa"
	"crypto/ed25519"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/base64"
	"encoding/json"
	"encoding/pem"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// File names under the state directory.
const (
	IdentityFile = "identity.json"
	KeyFile      = "tls.key"
	CertFile     = "tls.crt"
	CAFile       = "ca.crt"
)

// Identity is what enrollment produces.
type Identity struct {
	Controller string `json:"controller"`
	DeviceID   string `json:"device_id"`
	Name       string `json:"name"`
	Seed       string `json:"seed"` // base64 of the 32-byte Ed25519 seed
	LastTS     int64  `json:"last_ts"`
	PlainHTTP  bool   `json:"plain_http,omitempty"`
}

// SigningKey is the Ed25519 key that signs reports.
func (i Identity) SigningKey() (ed25519.PrivateKey, error) {
	seed, err := base64.StdEncoding.DecodeString(i.Seed)
	if err != nil || len(seed) != ed25519.SeedSize {
		return nil, errors.New("identity.json holds a damaged key")
	}
	return ed25519.NewKeyFromSeed(seed), nil
}

// DefaultDir is where the agent keeps its state unless told otherwise.
func DefaultDir() (string, error) {
	if dir := os.Getenv("SY_POSTURE_DIR"); dir != "" {
		return dir, nil
	}
	base, err := os.UserConfigDir()
	if err != nil {
		return "", err
	}
	return filepath.Join(base, "switchyard-posture"), nil
}

// WriteFile writes data atomically with the given mode.
func WriteFile(path string, data []byte, mode os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Chmod(mode); err != nil && !errors.Is(err, os.ErrPermission) {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), path)
}

// NewIdentity makes the signing key and the certificate key for a new device. Nothing is written
// until Save, after the controller has accepted the enrollment.
func NewIdentity(controller string, plainHTTP bool) (Identity, ed25519.PublicKey, *ecdsa.PrivateKey, error) {
	pub, priv, err := ed25519.GenerateKey(rand.Reader)
	if err != nil {
		return Identity{}, nil, nil, err
	}
	tlsKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return Identity{}, nil, nil, err
	}
	return Identity{Controller: controller, Seed: base64.StdEncoding.EncodeToString(priv.Seed()), PlainHTTP: plainHTTP}, pub, tlsKey, nil
}

// Save writes the identity and the certificate key.
func Save(dir string, id Identity, tlsKey *ecdsa.PrivateKey) error {
	der, err := x509.MarshalPKCS8PrivateKey(tlsKey)
	if err != nil {
		return err
	}
	if err := WriteFile(filepath.Join(dir, KeyFile), pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}), 0o600); err != nil {
		return err
	}
	return SaveIdentity(dir, id)
}

// SaveIdentity rewrites identity.json (for example with a newer LastTS).
func SaveIdentity(dir string, id Identity) error {
	raw, err := json.MarshalIndent(id, "", "  ")
	if err != nil {
		return err
	}
	return WriteFile(filepath.Join(dir, IdentityFile), append(raw, '\n'), 0o600)
}

// Load reads the identity.
func Load(dir string) (Identity, error) {
	raw, err := os.ReadFile(filepath.Join(dir, IdentityFile))
	if err != nil {
		return Identity{}, fmt.Errorf("this device is not enrolled (%w)", err)
	}
	var id Identity
	if err := json.Unmarshal(raw, &id); err != nil || id.DeviceID == "" || id.Controller == "" {
		return Identity{}, errors.New("identity.json is damaged; enroll again")
	}
	return id, nil
}

// CSR builds a certificate request for the device's certificate key. The controller ignores the
// subject and sets the names itself.
func CSR(dir, name string) (string, error) {
	raw, err := os.ReadFile(filepath.Join(dir, KeyFile))
	if err != nil {
		return "", err
	}
	block, _ := pem.Decode(raw)
	if block == nil {
		return "", errors.New("tls.key is damaged; enroll again")
	}
	parsed, err := x509.ParsePKCS8PrivateKey(block.Bytes)
	if err != nil {
		return "", err
	}
	der, err := x509.CreateCertificateRequest(rand.Reader, &x509.CertificateRequest{Subject: pkix.Name{CommonName: name}}, parsed)
	if err != nil {
		return "", err
	}
	return string(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE REQUEST", Bytes: der})), nil
}
