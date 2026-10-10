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
	"switchyard/agent/internal/runner"
	"switchyard/agent/internal/server"
	"switchyard/agent/internal/status"
	"switchyard/agent/internal/watchdog"
)

func main() {
	listen := flag.String("listen", ":7443", "address to listen on")
	root := flag.String("root", "/etc/switchyard", "managed directory root")
	certFile := flag.String("cert", "/etc/switchyard/agent/tls.crt", "server certificate")
	keyFile := flag.String("key", "/etc/switchyard/agent/tls.key", "server key")
	caFile := flag.String("ca", "/etc/switchyard/agent/ca.crt", "CA that signs controller client certificates")
	pubFile := flag.String("bundle-key", "/etc/switchyard/agent/bundle.pub", "base64 Ed25519 public key that signs bundles")
	stateDir := flag.String("state-dir", "/run/switchyard", "directory with vrrp-<name>.state files")
	healthURL := flag.String("health-url", "http://127.0.0.1:8404/healthz", "local HAProxy health endpoint")
	failures := flag.Int("withdraw-after", 3, "consecutive failed health checks before BGP prefixes are withdrawn")
	flag.Parse()

	pub, err := loadPublicKey(*pubFile)
	if err != nil {
		log.Fatalf("bundle key: %v", err)
	}
	tlsConfig, err := serverTLS(*certFile, *keyFile, *caFile)
	if err != nil {
		log.Fatalf("tls: %v", err)
	}

	run := runner.Default()
	applier := &bundle.Applier{Root: *root, PubKey: pub, Runner: run}
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
	collector := &status.Collector{
		Runner: run, StateDir: *stateDir, Hashes: applier.Hashes, BundleID: applier.LastBundleID,
		Health: wd.State, Now: time.Now,
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
	pb.RegisterAgentServer(grpcServer, &server.Server{Applier: applier, Collector: collector})
	go func() { <-ctx.Done(); grpcServer.GracefulStop() }()
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

func serverTLS(certFile, keyFile, caFile string) (*tls.Config, error) {
	cert, err := tls.LoadX509KeyPair(certFile, keyFile)
	if err != nil {
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
		Certificates: []tls.Certificate{cert},
		ClientCAs:    pool,
		ClientAuth:   tls.RequireAndVerifyClientCert,
		MinVersion:   tls.VersionTLS13,
	}, nil
}
