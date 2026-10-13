// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

// Package tunnel carries a local TCP connection to a service behind a Switchyard engine over TLS
// with the device's client certificate. It is the "ZTNA for non-HTTP applications" part of
// ADR 0196: SSH, RDP or a database client connects to 127.0.0.1, and the engine's TCP service
// (a TLS port that requires a client certificate from the device CA) admits only managed,
// compliant devices.
//
// The certificate is read from disk for every connection, so a renewal by `sy-posture run` is used
// by the next connection without restarting the tunnel.
package tunnel

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"sync"
	"time"
)

// Config describes one tunnel.
type Config struct {
	Listen     string // for example 127.0.0.1:2222
	To         string // host:port of the engine's TCP service
	ServerName string // TLS server name; default: the host of To
	CertFile   string
	KeyFile    string
	// ServerCA is an optional PEM file with the CA that signed the engine's certificate; without
	// it the system roots are used.
	ServerCA string
	// Logf receives one line per refused or failed connection; nil discards them.
	Logf func(format string, args ...any)
}

func (c Config) tlsConfig() (*tls.Config, error) {
	host, _, err := net.SplitHostPort(c.To)
	if err != nil {
		return nil, fmt.Errorf("--to must be host:port: %w", err)
	}
	cfg := &tls.Config{
		MinVersion: tls.VersionTLS12,
		ServerName: host,
		GetClientCertificate: func(*tls.CertificateRequestInfo) (*tls.Certificate, error) {
			cert, err := tls.LoadX509KeyPair(c.CertFile, c.KeyFile)
			if err != nil {
				return nil, fmt.Errorf("no device certificate yet (run sy-posture report): %w", err)
			}
			return &cert, nil
		},
	}
	if c.ServerName != "" {
		cfg.ServerName = c.ServerName
	}
	if c.ServerCA != "" {
		pem, err := os.ReadFile(c.ServerCA)
		if err != nil {
			return nil, err
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pem) {
			return nil, errors.New("--server-ca holds no certificate")
		}
		cfg.RootCAs = pool
	}
	return cfg, nil
}

// Serve accepts connections on the listener until ctx ends.
func Serve(ctx context.Context, ln net.Listener, c Config) error {
	cfg, err := c.tlsConfig()
	if err != nil {
		return err
	}
	logf := c.Logf
	if logf == nil {
		logf = func(string, ...any) {}
	}
	go func() {
		<-ctx.Done()
		ln.Close()
	}()
	var wg sync.WaitGroup
	defer wg.Wait()
	for {
		conn, err := ln.Accept()
		if err != nil {
			if ctx.Err() != nil {
				return nil
			}
			return err
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			defer conn.Close()
			dialer := &tls.Dialer{NetDialer: &net.Dialer{Timeout: 10 * time.Second}, Config: cfg}
			up, err := dialer.DialContext(ctx, "tcp", c.To)
			if err != nil {
				logf("tunnel to %s failed: %v", c.To, err)
				return
			}
			defer up.Close()
			pipe(conn, up)
		}()
	}
}

// pipe copies both ways until one side closes.
func pipe(a, b net.Conn) {
	done := make(chan struct{}, 2)
	cp := func(dst, src net.Conn) {
		_, _ = io.Copy(dst, src)
		if cw, ok := dst.(interface{ CloseWrite() error }); ok {
			_ = cw.CloseWrite()
		}
		done <- struct{}{}
	}
	go cp(a, b)
	go cp(b, a)
	<-done
	<-done
}
