// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

package state

import (
	"crypto/ed25519"
	"crypto/x509"
	"encoding/pem"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

func TestSaveLoadAndSigningKey(t *testing.T) {
	dir := t.TempDir()
	id, pub, tlsKey, err := NewIdentity("https://c.example.test", false)
	if err != nil {
		t.Fatal(err)
	}
	id.DeviceID, id.Name = "d1", "laptop"
	if err := Save(dir, id, tlsKey); err != nil {
		t.Fatal(err)
	}
	got, err := Load(dir)
	if err != nil || got.DeviceID != "d1" || got.Controller != "https://c.example.test" {
		t.Fatalf("%+v %v", got, err)
	}
	key, err := got.SigningKey()
	if err != nil || string(key.Public().(ed25519.PublicKey)) != string(pub) {
		t.Fatalf("the saved seed must give back the enrolled public key: %v", err)
	}
	if runtime.GOOS != "windows" {
		for _, f := range []string{IdentityFile, KeyFile} {
			info, err := os.Stat(filepath.Join(dir, f))
			if err != nil || info.Mode().Perm() != 0o600 {
				t.Errorf("%s must be owner-only: %v %v", f, info, err)
			}
		}
	}
}

func TestCSRCarriesOnlyThePublicKey(t *testing.T) {
	dir := t.TempDir()
	id, _, tlsKey, _ := NewIdentity("https://c.example.test", false)
	id.DeviceID = "d1"
	if err := Save(dir, id, tlsKey); err != nil {
		t.Fatal(err)
	}
	csrPEM, err := CSR(dir, "laptop")
	if err != nil || strings.Contains(csrPEM, "PRIVATE") {
		t.Fatalf("%v", err)
	}
	block, _ := pem.Decode([]byte(csrPEM))
	csr, err := x509.ParseCertificateRequest(block.Bytes)
	if err != nil || csr.CheckSignature() != nil || csr.Subject.CommonName != "laptop" {
		t.Fatalf("%v", err)
	}
}

func TestDamagedOrMissingStateIsReported(t *testing.T) {
	dir := t.TempDir()
	if _, err := Load(dir); err == nil || !strings.Contains(err.Error(), "not enrolled") {
		t.Fatalf("%v", err)
	}
	os.WriteFile(filepath.Join(dir, IdentityFile), []byte("{"), 0o600)
	if _, err := Load(dir); err == nil {
		t.Fatal("damaged identity")
	}
	if _, err := (Identity{Seed: "AAAA"}).SigningKey(); err == nil {
		t.Fatal("short seed")
	}
	if _, err := CSR(dir, "x"); err == nil {
		t.Fatal("no key")
	}
	os.WriteFile(filepath.Join(dir, KeyFile), []byte("junk"), 0o600)
	if _, err := CSR(dir, "x"); err == nil {
		t.Fatal("damaged key")
	}
}

func TestDefaultDirHonoursTheEnvironment(t *testing.T) {
	t.Setenv("SY_POSTURE_DIR", "/somewhere")
	if d, _ := DefaultDir(); d != "/somewhere" {
		t.Fatal(d)
	}
}

func TestWriteFileReplacesAtomically(t *testing.T) {
	p := filepath.Join(t.TempDir(), "sub", "f")
	if err := WriteFile(p, []byte("one"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := WriteFile(p, []byte("two"), 0o644); err != nil {
		t.Fatal(err)
	}
	if b, _ := os.ReadFile(p); string(b) != "two" {
		t.Fatal(string(b))
	}
	entries, _ := os.ReadDir(filepath.Dir(p))
	if len(entries) != 1 {
		t.Fatal("no temporary files may be left behind")
	}
}
