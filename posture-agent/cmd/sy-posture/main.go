// Command sy-posture is the Switchyard device posture agent (ADR 0196).
//
//	sy-posture enroll --controller URL --token T --name laptop-ana
//	sy-posture report            # measure, report, store the certificate it gets back
//	sy-posture run               # report on the controller's schedule, forever
//	sy-posture status            # what this device knows about itself
//	sy-posture tunnel --listen 127.0.0.1:2222 --to engine.example.test:8443
//
// The agent keeps its state in one directory (default: the user's config directory, or
// $SY_POSTURE_DIR, or --dir). It never sends a private key anywhere: reports are signed with an
// Ed25519 key, the certificate request carries only the public half of the TLS key.
package main

import (
	"context"
	"crypto/x509"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"time"

	"switchyard/posture-agent/internal/facts"
	"switchyard/posture-agent/internal/protocol"
	"switchyard/posture-agent/internal/state"
	"switchyard/posture-agent/internal/tunnel"
)

// measure is replaced by tests; the real one runs the operating system's own tools.
var measure = func(ctx context.Context) (protocol.Facts, error) {
	return facts.Collect(ctx, facts.System())
}

const (
	exitNonCompliant = 3
	minInterval      = 60 * time.Second
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 {
		fmt.Fprintln(stderr, "usage: sy-posture enroll|report|run|status|tunnel [flags]")
		return 2
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	var err error
	code := 0
	switch args[0] {
	case "enroll":
		err = enroll(ctx, args[1:], stdout)
	case "report":
		code, err = report(ctx, args[1:], stdout)
	case "run":
		err = loop(ctx, args[1:], stdout)
	case "status":
		err = status(args[1:], stdout)
	case "tunnel":
		err = runTunnel(ctx, args[1:], stderr)
	default:
		fmt.Fprintf(stderr, "unknown command %q\n", args[0])
		return 2
	}
	if err != nil {
		fmt.Fprintln(stderr, "error:", err)
		return 1
	}
	return code
}

type common struct {
	dir, controllerCA string
	plainHTTP         bool
}

func (c *common) flags(fs *flag.FlagSet) {
	fs.StringVar(&c.dir, "dir", "", "state directory (default: $SY_POSTURE_DIR or the user config directory)")
	fs.StringVar(&c.controllerCA, "controller-ca", "", "PEM file with the CA of the controller's TLS certificate (default: system roots)")
	fs.BoolVar(&c.plainHTTP, "insecure-plain-http", false, "allow an http:// controller URL (tests only)")
}

func (c *common) resolve() error {
	if c.dir == "" {
		d, err := state.DefaultDir()
		if err != nil {
			return err
		}
		c.dir = d
	}
	return nil
}

func (c *common) client(url string, plain bool) (*protocol.Client, error) {
	httpClient := &http.Client{Timeout: 30 * time.Second}
	if c.controllerCA != "" {
		pem, err := os.ReadFile(c.controllerCA)
		if err != nil {
			return nil, err
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pem) {
			return nil, errors.New("--controller-ca holds no certificate")
		}
		tr := http.DefaultTransport.(*http.Transport).Clone()
		tr.TLSClientConfig.RootCAs = pool
		httpClient.Transport = tr
	}
	return protocol.NewClient(url, httpClient, plain || c.plainHTTP)
}

func enroll(ctx context.Context, args []string, stdout io.Writer) error {
	var c common
	fs := flag.NewFlagSet("enroll", flag.ContinueOnError)
	c.flags(fs)
	controller := fs.String("controller", "", "controller base URL, for example https://switchyard.example.test")
	token := fs.String("token", "", "one-time enrollment token")
	name := fs.String("name", "", "device name (default: the host name)")
	owner := fs.String("owner", "", "who the device belongs to")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if err := c.resolve(); err != nil {
		return err
	}
	if *controller == "" || *token == "" {
		return errors.New("--controller and --token are required")
	}
	if *name == "" {
		h, err := os.Hostname()
		if err != nil {
			return err
		}
		*name = h
	}
	client, err := c.client(*controller, false)
	if err != nil {
		return err
	}
	id, pub, tlsKey, err := state.NewIdentity(strings.TrimRight(*controller, "/"), c.plainHTTP)
	if err != nil {
		return err
	}
	result, err := client.Enroll(ctx, *token, *name, pub, *owner)
	if err != nil {
		return err
	}
	id.DeviceID, id.Name = result.DeviceID, result.Name
	if err := state.Save(c.dir, id, tlsKey); err != nil {
		return err
	}
	fmt.Fprintf(stdout, "enrolled as %s in %s (policy %s, certificates last %d h)\n", result.Name, result.Tenant, result.Policy, result.CertTTLH)
	return nil
}

// reportOnce measures, reports and stores the certificate. It returns the result.
func reportOnce(ctx context.Context, c *common) (protocol.ReportResult, error) {
	id, err := state.Load(c.dir)
	if err != nil {
		return protocol.ReportResult{}, err
	}
	key, err := id.SigningKey()
	if err != nil {
		return protocol.ReportResult{}, err
	}
	f, err := measure(ctx)
	if err != nil {
		return protocol.ReportResult{}, err
	}
	csr, err := state.CSR(c.dir, id.Name)
	if err != nil {
		return protocol.ReportResult{}, err
	}
	client, err := c.client(id.Controller, id.PlainHTTP)
	if err != nil {
		return protocol.ReportResult{}, err
	}
	result, ts, err := client.SendReport(ctx, id.DeviceID, key, protocol.Report{Facts: f, CSR: csr}, id.LastTS)
	if err != nil {
		return result, err
	}
	id.LastTS = ts
	if err := state.SaveIdentity(c.dir, id); err != nil {
		return result, err
	}
	if result.Certificate != "" {
		if block, _ := pem.Decode([]byte(result.Certificate)); block == nil {
			return result, errors.New("the controller sent a certificate that cannot be read")
		}
		if err := state.WriteFile(filepath.Join(c.dir, state.CertFile), []byte(result.Certificate), 0o644); err != nil {
			return result, err
		}
		if err := state.WriteFile(filepath.Join(c.dir, state.CAFile), []byte(result.CA), 0o644); err != nil {
			return result, err
		}
	}
	return result, nil
}

func describe(r protocol.ReportResult) string {
	if r.Compliant {
		return fmt.Sprintf("compliant; certificate valid until %s", r.NotAfter)
	}
	return "not compliant: " + strings.Join(r.Failures, ", ")
}

func report(ctx context.Context, args []string, stdout io.Writer) (int, error) {
	var c common
	fs := flag.NewFlagSet("report", flag.ContinueOnError)
	c.flags(fs)
	if err := fs.Parse(args); err != nil {
		return 0, err
	}
	if err := c.resolve(); err != nil {
		return 0, err
	}
	result, err := reportOnce(ctx, &c)
	if err != nil {
		return 0, err
	}
	fmt.Fprintln(stdout, describe(result))
	if !result.Compliant {
		return exitNonCompliant, nil
	}
	return 0, nil
}

func loop(ctx context.Context, args []string, stdout io.Writer) error {
	var c common
	fs := flag.NewFlagSet("run", flag.ContinueOnError)
	c.flags(fs)
	if err := fs.Parse(args); err != nil {
		return err
	}
	if err := c.resolve(); err != nil {
		return err
	}
	wait := time.Second
	for {
		result, err := reportOnce(ctx, &c)
		if err != nil {
			fmt.Fprintln(stdout, time.Now().Format(time.RFC3339), "error:", err)
			wait = min(max(wait*2, minInterval), 15*time.Minute)
		} else {
			fmt.Fprintln(stdout, time.Now().Format(time.RFC3339), describe(result))
			wait = max(time.Duration(result.NextReportS)*time.Second, minInterval)
		}
		select {
		case <-ctx.Done():
			return nil
		case <-time.After(wait):
		}
	}
}

func status(args []string, stdout io.Writer) error {
	var c common
	fs := flag.NewFlagSet("status", flag.ContinueOnError)
	c.flags(fs)
	if err := fs.Parse(args); err != nil {
		return err
	}
	if err := c.resolve(); err != nil {
		return err
	}
	id, err := state.Load(c.dir)
	if err != nil {
		return err
	}
	fmt.Fprintf(stdout, "device %s (%s) enrolled at %s\n", id.Name, id.DeviceID, id.Controller)
	raw, err := os.ReadFile(filepath.Join(c.dir, state.CertFile))
	if err != nil {
		fmt.Fprintln(stdout, "no certificate yet")
		return nil
	}
	block, _ := pem.Decode(raw)
	if block == nil {
		return errors.New("tls.crt is damaged")
	}
	cert, err := x509.ParseCertificate(block.Bytes)
	if err != nil {
		return err
	}
	left := time.Until(cert.NotAfter).Round(time.Minute)
	state := "valid"
	if left <= 0 {
		state = "EXPIRED"
	}
	fmt.Fprintf(stdout, "certificate %s, expires %s (%s)\n", state, cert.NotAfter.Format(time.RFC3339), left)
	return nil
}

func runTunnel(ctx context.Context, args []string, stderr io.Writer) error {
	var c common
	fs := flag.NewFlagSet("tunnel", flag.ContinueOnError)
	c.flags(fs)
	listen := fs.String("listen", "127.0.0.1:2222", "local address to listen on")
	to := fs.String("to", "", "host:port of the TCP service behind the engine")
	serverName := fs.String("server-name", "", "TLS server name (default: the host of --to)")
	serverCA := fs.String("server-ca", "", "PEM file with the CA of the engine's certificate (default: system roots)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	if err := c.resolve(); err != nil {
		return err
	}
	if *to == "" {
		return errors.New("--to is required")
	}
	if host, _, err := net.SplitHostPort(*listen); err != nil || (host != "127.0.0.1" && host != "::1" && host != "localhost") {
		return errors.New("--listen must be a loopback address: the tunnel carries your device's identity")
	}
	ln, err := net.Listen("tcp", *listen)
	if err != nil {
		return err
	}
	fmt.Fprintf(stderr, "tunnel: %s -> %s\n", ln.Addr(), *to)
	return tunnel.Serve(ctx, ln, tunnel.Config{
		Listen: *listen, To: *to, ServerName: *serverName, ServerCA: *serverCA,
		CertFile: filepath.Join(c.dir, state.CertFile), KeyFile: filepath.Join(c.dir, state.KeyFile),
		Logf: func(format string, args ...any) { fmt.Fprintf(stderr, format+"\n", args...) },
	})
}
