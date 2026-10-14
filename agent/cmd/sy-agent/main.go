// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

// Command sy-agent is the Switchyard SE agent: a small gRPC server (mTLS) that applies signed
// configuration bundles for FRR, Keepalived and Coraza, reports status, and withdraws BGP routes
// while the local HAProxy is unhealthy.
package main

import (
	"context"
	"crypto/ed25519"
	"crypto/tls"
	"crypto/x509"
	"encoding/base64"
	"encoding/pem"
	"errors"
	"flag"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"

	pb "switchyard/agent/gen/switchyard/agent/v1"
	"switchyard/agent/internal/bundle"
	"switchyard/agent/internal/capture"
	"switchyard/agent/internal/enroll"
	"switchyard/agent/internal/ipvs"
	"switchyard/agent/internal/runner"
	"switchyard/agent/internal/server"
	"switchyard/agent/internal/status"
	"switchyard/agent/internal/watchdog"
)

const defaultControllerCN = "switchyard-controller"

func main() {
	if len(os.Args) > 1 && (os.Args[1] == "enroll" || os.Args[1] == "renew") {
		os.Exit(runEnroll(os.Args[1], os.Args[2:]))
	}
	listen := flag.String("listen", ":7443", "address to listen on")
	root := flag.String("root", "/etc/switchyard", "managed directory root")
	certFile := flag.String("cert", "/etc/switchyard/agent/tls.crt", "server certificate")
	keyFile := flag.String("key", "/etc/switchyard/agent/tls.key", "server key")
	caFile := flag.String("ca", "/etc/switchyard/agent/ca.crt", "CA that signs controller client certificates")
	pubFile := flag.String("bundle-key", "/etc/switchyard/agent/bundle.pub", "base64 Ed25519 public key that signs bundles")
	stateDir := flag.String("state-dir", "/run/switchyard", "directory with vrrp-<name>.state files")
	healthURL := flag.String("health-url", "http://127.0.0.1:8404/healthz", "local HAProxy health endpoint")
	allowUnbound := flag.Bool("allow-unbound-bundles", false, "accept bundles without an engine name and sequence (controllers older than ADR 0074; they can be replayed)")
	controllerCN := flag.String("controller-cn", defaultControllerCN, "comma-separated certificate names allowed to call the agent (the controller's client certificate); explicitly empty allows any certificate the CA signed")
	failures := flag.Int("withdraw-after", 3, "consecutive failed health checks before BGP prefixes are withdrawn")
	flag.Parse()

	pub, err := loadPublicKey(*pubFile)
	if err != nil {
		log.Fatalf("bundle key: %v", err)
	}
	tlsConfig, err := serverTLS(*certFile, *keyFile, *caFile, splitNames(*controllerCN))
	if err != nil {
		log.Fatalf("tls: %v", err)
	}
	if *controllerCN == "" {
		log.Printf("warning: -controller-cn is not set; any certificate signed by the CA may call this agent")
	}
	engineName := certCommonName(*certFile)

	run := runner.Default()
	applier := &bundle.Applier{Root: *root, PubKey: pub, Runner: run, Engine: engineName, AllowUnbound: *allowUnbound}
	client := &http.Client{Timeout: 2 * time.Second}
	wd := &watchdog.Watchdog{
		Runner:    run,
		VIPsFile:  filepath.Join(*root, "frr", "vips.json"),
		Threshold: *failures,
		Check: func(ctx context.Context) bool {
			req, _ := http.NewRequestWithContext(ctx, http.MethodGet, *healthURL, nil)
			resp, err := client.Do(req)
			if err != nil {
				return false
			}
			defer resp.Body.Close()
			return resp.StatusCode == http.StatusOK
		},
	}
	keepalivedConf := filepath.Join(*root, "keepalived", "keepalived.conf")
	wd.UDPUnhealthy = func(ctx context.Context) map[string]bool {
		servers, err := ipvs.Collect(ctx, run, keepalivedConf)
		if err != nil {
			log.Printf("ipvs: %v", err)
			return nil // unknown is not unhealthy: do not withdraw routes on a failed listing
		}
		_, aerr := run.Run(ctx, "systemctl", "is-active", "--quiet", "keepalived")
		return ipvs.UnhealthyVIPs(servers, aerr == nil)
	}
	collector := &status.Collector{
		Runner: run, StateDir: *stateDir, Hashes: applier.Hashes, BundleID: applier.LastBundleID,
		Health: wd.State, Withdrawn: wd.Withdrawn, Now: time.Now, KeepalivedConf: keepalivedConf,
		WafConfig: filepath.Join(*root, "coraza", "config.yaml"),
	}

	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer stop()
	go func() {
		t := time.NewTicker(2 * time.Second)
		defer t.Stop()
		for {
			if err := wd.Tick(ctx); err != nil {
				log.Printf("watchdog: %v", err)
			}
			select {
			case <-ctx.Done():
				return
			case <-t.C:
			}
		}
	}()

	lis, err := net.Listen("tcp", *listen)
	if err != nil {
		log.Fatal(err)
	}
	grpcServer := grpc.NewServer(grpc.Creds(credentials.NewTLS(tlsConfig)))
	pb.RegisterAgentServer(grpcServer, &server.Server{Applier: applier, Collector: collector, Root: *root, Capturer: capture.New()})
	go func() {
		<-ctx.Done()
		// Watch streams only end when the controller disconnects, so a graceful stop alone can wait until systemd
		// kills the unit (90 seconds); give in-flight calls a few seconds, then close everything.
		done := make(chan struct{})
		go func() { grpcServer.GracefulStop(); close(done) }()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			grpcServer.Stop()
		}
	}()
	log.Printf("sy-agent %s listening on %s", status.Version, *listen)
	if err := grpcServer.Serve(lis); err != nil {
		log.Fatal(err)
	}
}

func loadPublicKey(path string) (ed25519.PublicKey, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	key, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(raw)))
	if err != nil || len(key) != ed25519.PublicKeySize {
		return nil, os.ErrInvalid
	}
	return ed25519.PublicKey(key), nil
}

// splitNames turns "a, b" into ["a", "b"] and "" into nil.
func splitNames(list string) []string {
	var out []string
	for _, part := range strings.Split(list, ",") {
		if part = strings.TrimSpace(part); part != "" {
			out = append(out, part)
		}
	}
	return out
}

// certCommonName returns the common name of the first certificate in a PEM file, or "" (the engine
// name the controller put in this agent's certificate).
func certCommonName(path string) string {
	raw, err := os.ReadFile(path)
	if err != nil {
		return ""
	}
	block, _ := pem.Decode(raw)
	if block == nil {
		return ""
	}
	cert, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		return ""
	}
	return cert.Subject.CommonName
}

// allowedClient checks the verified client certificate against the allowed names (CN or DNS SAN).
func allowedClient(allowed []string) func([][]byte, [][]*x509.Certificate) error {
	return func(_ [][]byte, chains [][]*x509.Certificate) error {
		if len(allowed) == 0 {
			return nil
		}
		for _, chain := range chains {
			leaf := chain[0]
			names := append([]string{leaf.Subject.CommonName}, leaf.DNSNames...)
			for _, name := range names {
				for _, ok := range allowed {
					if name == ok {
						return nil
					}
				}
			}
		}
		return errors.New("client certificate name is not allowed to call this agent")
	}
}

func serverTLS(certFile, keyFile, caFile string, allowed []string) (*tls.Config, error) {
	reloader := &enroll.Reloader{CertPath: certFile, KeyPath: keyFile}
	if _, err := reloader.GetCertificate(nil); err != nil {
		return nil, err
	}
	caPEM, err := os.ReadFile(caFile)
	if err != nil {
		return nil, err
	}
	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(caPEM) {
		return nil, os.ErrInvalid
	}
	return &tls.Config{
		GetCertificate: reloader.GetCertificate, // renewed certificates are picked up without a restart
		ClientCAs:      pool,
		ClientAuth:     tls.RequireAndVerifyClientCert,
		MinVersion:     tls.VersionTLS13,
		// Runs after the chain was verified against the CA: only the named certificates may call.
		VerifyPeerCertificate: allowedClient(allowed),
	}, nil
}

// runEnroll implements "sy-agent enroll" (first certificate, with a one-time token) and
// "sy-agent renew" (with the rotating renewal token saved by the previous run).
func runEnroll(mode string, args []string) int {
	fs := flag.NewFlagSet(mode, flag.ExitOnError)
	controller := fs.String("controller", "", "controller base URL, for example https://switchyard.example.test")
	token := fs.String("token", "", "one-time enrollment token")
	tokenFile := fs.String("token-file", "", "file holding the enrollment token (cloud-init writes it)")
	dir := fs.String("dir", "/etc/switchyard/agent", "agent state directory")
	plain := fs.Bool("insecure-plain-http", false, "allow an http:// controller URL (throwaway labs only: the answer carries the CA and the bundle signing key)")
	trust := fs.String("controller-ca", "", "CA file that signs the controller's HTTPS certificate (default: system roots)")
	_ = fs.Parse(args)

	value := strings.TrimSpace(*token)
	switch {
	case mode == "renew":
		raw, err := os.ReadFile(filepath.Join(*dir, enroll.RenewTokenFile))
		if err != nil {
			log.Printf("renew: %v", err)
			return 1
		}
		value = strings.TrimSpace(string(raw))
	case value == "" && *tokenFile != "":
		raw, err := os.ReadFile(*tokenFile)
		if err != nil {
			log.Printf("enroll: %v", err)
			return 1
		}
		value = strings.TrimSpace(string(raw))
	}
	httpClient := &http.Client{Timeout: 30 * time.Second}
	if *trust != "" {
		pemBytes, err := os.ReadFile(*trust)
		pool := x509.NewCertPool()
		if err != nil || !pool.AppendCertsFromPEM(pemBytes) {
			log.Printf("enroll: cannot use controller CA %s", *trust)
			return 1
		}
		httpClient.Transport = &http.Transport{TLSClientConfig: &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12}}
	}
	host, _ := os.Hostname()
	engine, err := enroll.Run(context.Background(), enroll.Config{
		ControllerURL: *controller, Token: value, Dir: *dir, Hostname: host, HTTP: httpClient,
		AllowPlainHTTP: *plain,
	})
	if err != nil {
		log.Printf("%s: %v", mode, err)
		return 1
	}
	if *tokenFile != "" && mode == "enroll" {
		_ = os.Remove(*tokenFile) // the token is single use; do not leave it lying around
	}
	log.Printf("%s: certificate for engine %s saved in %s", mode, engine, *dir)
	return 0
}
