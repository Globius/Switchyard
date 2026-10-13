package tunnel

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"io"
	"math/big"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

type authority struct {
	cert *x509.Certificate
	key  *ecdsa.PrivateKey
	pem  []byte
}

func newCA(t *testing.T, name string) authority {
	t.Helper()
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	tpl := &x509.Certificate{
		SerialNumber: big.NewInt(1), Subject: pkix.Name{CommonName: name},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: time.Now().Add(time.Hour),
		IsCA: true, BasicConstraintsValid: true, KeyUsage: x509.KeyUsageCertSign,
	}
	der, err := x509.CreateCertificate(rand.Reader, tpl, tpl, &key.PublicKey, key)
	if err != nil {
		t.Fatal(err)
	}
	cert, _ := x509.ParseCertificate(der)
	return authority{cert, key, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der})}
}

// issue signs a leaf for usage and returns its PEM certificate and key.
func (a authority) issue(t *testing.T, cn string, usage x509.ExtKeyUsage, notAfter time.Time) ([]byte, []byte) {
	t.Helper()
	key, _ := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	tpl := &x509.Certificate{
		SerialNumber: big.NewInt(time.Now().UnixNano()), Subject: pkix.Name{CommonName: cn},
		NotBefore: time.Now().Add(-time.Hour), NotAfter: notAfter,
		ExtKeyUsage: []x509.ExtKeyUsage{usage}, DNSNames: []string{"localhost"},
		IPAddresses: []net.IP{net.ParseIP("127.0.0.1")},
	}
	der, err := x509.CreateCertificate(rand.Reader, tpl, a.cert, &key.PublicKey, a.key)
	if err != nil {
		t.Fatal(err)
	}
	kder, _ := x509.MarshalPKCS8PrivateKey(key)
	return pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: kder})
}

// engine is a TLS echo service that, like an engine's TCP port, requires a client certificate
// from the device CA.
func engine(t *testing.T, serverCA, deviceCA authority) string {
	t.Helper()
	crt, key := serverCA.issue(t, "engine", x509.ExtKeyUsageServerAuth, time.Now().Add(time.Hour))
	pair, err := tls.X509KeyPair(crt, key)
	if err != nil {
		t.Fatal(err)
	}
	pool := x509.NewCertPool()
	pool.AddCert(deviceCA.cert)
	ln, err := tls.Listen("tcp", "127.0.0.1:0", &tls.Config{
		Certificates: []tls.Certificate{pair}, ClientAuth: tls.RequireAndVerifyClientCert, ClientCAs: pool,
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func() {
				defer c.Close()
				if tc, ok := c.(*tls.Conn); ok && tc.Handshake() != nil {
					return
				}
				io.Copy(c, c)
			}()
		}
	}()
	return ln.Addr().String()
}

func start(t *testing.T, cfg Config) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- Serve(ctx, ln, cfg) }()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Error("the tunnel did not stop")
		}
	})
	return ln.Addr().String()
}

func echo(t *testing.T, addr, msg string) (string, error) {
	t.Helper()
	c, err := net.DialTimeout("tcp", addr, 3*time.Second)
	if err != nil {
		return "", err
	}
	defer c.Close()
	c.SetDeadline(time.Now().Add(5 * time.Second))
	if _, err := c.Write([]byte(msg)); err != nil {
		return "", err
	}
	buf := make([]byte, len(msg))
	_, err = io.ReadFull(c, buf)
	return string(buf), err
}

func write(t *testing.T, dir, name string, data []byte) string {
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, data, 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestADeviceWithACertificateFromTheDeviceCAGetsThrough(t *testing.T) {
	serverCA, deviceCA := newCA(t, "server"), newCA(t, "devices")
	target := engine(t, serverCA, deviceCA)
	dir := t.TempDir()
	crt, key := deviceCA.issue(t, "laptop", x509.ExtKeyUsageClientAuth, time.Now().Add(time.Hour))
	cfg := Config{
		To: target, ServerName: "localhost", ServerCA: write(t, dir, "server-ca.pem", serverCA.pem),
		CertFile: write(t, dir, "tls.crt", crt), KeyFile: write(t, dir, "tls.key", key),
	}
	addr := start(t, cfg)
	got, err := echo(t, addr, "hello through the tunnel")
	if err != nil || got != "hello through the tunnel" {
		t.Fatalf("%q %v", got, err)
	}
}

func TestACertificateFromAnotherCAIsRefusedByTheEngine(t *testing.T) {
	serverCA, deviceCA, other := newCA(t, "server"), newCA(t, "devices"), newCA(t, "other")
	target := engine(t, serverCA, deviceCA)
	dir := t.TempDir()
	crt, key := other.issue(t, "intruder", x509.ExtKeyUsageClientAuth, time.Now().Add(time.Hour))
	addr := start(t, Config{
		To: target, ServerName: "localhost", ServerCA: write(t, dir, "ca.pem", serverCA.pem),
		CertFile: write(t, dir, "tls.crt", crt), KeyFile: write(t, dir, "tls.key", key),
	})
	if got, err := echo(t, addr, "x"); err == nil {
		t.Fatalf("the engine must not talk to an unknown device, got %q", got)
	}
}

func TestAnExpiredCertificateAndAMissingOneAreRefused(t *testing.T) {
	serverCA, deviceCA := newCA(t, "server"), newCA(t, "devices")
	target := engine(t, serverCA, deviceCA)
	dir := t.TempDir()
	crt, key := deviceCA.issue(t, "laptop", x509.ExtKeyUsageClientAuth, time.Now().Add(-time.Minute))
	cfg := Config{
		To: target, ServerName: "localhost", ServerCA: write(t, dir, "ca.pem", serverCA.pem),
		CertFile: filepath.Join(dir, "tls.crt"), KeyFile: filepath.Join(dir, "tls.key"),
	}
	addr := start(t, cfg)
	if _, err := echo(t, addr, "x"); err == nil {
		t.Fatal("no certificate yet")
	}
	// a renewal on disk is used by the next connection without restarting the tunnel
	fresh, freshKey := deviceCA.issue(t, "laptop", x509.ExtKeyUsageClientAuth, time.Now().Add(time.Hour))
	write(t, dir, "tls.crt", crt)
	write(t, dir, "tls.key", key)
	if _, err := echo(t, addr, "x"); err == nil {
		t.Fatal("expired")
	}
	write(t, dir, "tls.crt", fresh)
	write(t, dir, "tls.key", freshKey)
	if got, err := echo(t, addr, "again"); err != nil || got != "again" {
		t.Fatalf("%q %v", got, err)
	}
}

func TestAServerWeDoNotTrustIsRefused(t *testing.T) {
	serverCA, deviceCA, wrong := newCA(t, "server"), newCA(t, "devices"), newCA(t, "wrong")
	target := engine(t, serverCA, deviceCA)
	dir := t.TempDir()
	crt, key := deviceCA.issue(t, "laptop", x509.ExtKeyUsageClientAuth, time.Now().Add(time.Hour))
	addr := start(t, Config{
		To: target, ServerName: "localhost", ServerCA: write(t, dir, "ca.pem", wrong.pem),
		CertFile: write(t, dir, "tls.crt", crt), KeyFile: write(t, dir, "tls.key", key),
	})
	if _, err := echo(t, addr, "x"); err == nil {
		t.Fatal("the device must not hand its identity to an untrusted server")
	}
}

func TestBadConfigurationStopsServe(t *testing.T) {
	ln, _ := net.Listen("tcp", "127.0.0.1:0")
	defer ln.Close()
	if err := Serve(context.Background(), ln, Config{To: "no-port"}); err == nil || !strings.Contains(err.Error(), "host:port") {
		t.Fatalf("%v", err)
	}
	if err := Serve(context.Background(), ln, Config{To: "h:1", ServerCA: filepath.Join(t.TempDir(), "missing")}); err == nil {
		t.Fatal("missing CA file")
	}
	bad := filepath.Join(t.TempDir(), "ca.pem")
	os.WriteFile(bad, []byte("not pem"), 0o600)
	if err := Serve(context.Background(), ln, Config{To: "h:1", ServerCA: bad}); err == nil {
		t.Fatal("CA file without certificates")
	}
}
