// Package probe runs one health check against a pool member from this engine.
//
// It is deliberately narrow: the target must already be a server of the applied HAProxy
// configuration or a real server of the applied Keepalived configuration, so the RPC cannot be used
// to scan the network.
package probe

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"net"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// Request is one probe.
type Request struct {
	Protocol     string
	Address      string
	Port         uint32
	Timeout      time.Duration
	HTTPMethod   string
	HTTPPath     string
	HTTPHost     string
	ExpectStatus []uint32
	UDPPayload   []byte
	UDPExpect    []byte
}

// Result is the outcome of a probe.
type Result struct {
	OK      bool
	Latency time.Duration
	Detail  string
	Refused bool
}

var (
	ipvsReal = regexp.MustCompile(`^\s*real_server\s+(\S+)\s+(\d+)`)
	pathOK   = regexp.MustCompile(`^/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*$`)
	hostOK   = regexp.MustCompile(`^[A-Za-z0-9.-]{1,253}$`)
)

// haproxyTargets returns the address:port targets of one "server NAME ADDR[:PORT] ... [port N]" line.
func haproxyTargets(line string) []string {
	f := strings.Fields(line)
	if len(f) < 3 || f[0] != "server" {
		return nil
	}
	target := f[2]
	for _, p := range []string{"ipv4@", "ipv6@"} {
		target = strings.TrimPrefix(target, p)
	}
	addr, port := target, ""
	if i := strings.LastIndex(target, ":"); i >= 0 {
		// "addr:port"; a bare IPv6 address without a port is the fallback.
		if left := strings.Trim(target[:i], "[]"); net.ParseIP(left) != nil {
			addr, port = left, target[i+1:]
		}
	}
	if net.ParseIP(addr) == nil {
		return nil
	}
	var out []string
	if port != "" {
		out = append(out, net.JoinHostPort(addr, port))
	}
	for i := 3; i+1 < len(f); i++ {
		if f[i] == "port" {
			out = append(out, net.JoinHostPort(addr, f[i+1]))
		}
	}
	return out
}

// Members returns the "address:port" targets named by the applied configuration files.
// A missing file contributes nothing.
func Members(haproxyCfg, keepalivedConf string) map[string]bool {
	out := map[string]bool{}
	read := func(path string, each func(string)) {
		f, err := os.Open(path)
		if err != nil {
			return
		}
		defer f.Close()
		sc := bufio.NewScanner(f)
		for sc.Scan() {
			each(sc.Text())
		}
	}
	read(haproxyCfg, func(line string) {
		for _, t := range haproxyTargets(line) {
			out[t] = true
		}
	})
	read(keepalivedConf, func(line string) {
		if m := ipvsReal.FindStringSubmatch(line); m != nil {
			out[net.JoinHostPort(strings.Trim(m[1], "[]"), m[2])] = true
		}
	})
	return out
}

// Run validates the request against the allowed members and performs it.
func Run(ctx context.Context, req Request, allowed map[string]bool) Result {
	target := net.JoinHostPort(req.Address, strconv.FormatUint(uint64(req.Port), 10))
	if net.ParseIP(req.Address) == nil || !allowed[target] {
		return Result{Refused: true, Detail: "target is not a member of the applied configuration"}
	}
	if req.Timeout < 100*time.Millisecond || req.Timeout > 10*time.Second {
		return Result{Detail: "timeout must be between 100 ms and 10 s"}
	}
	ctx, cancel := context.WithTimeout(ctx, req.Timeout)
	defer cancel()
	start := time.Now()
	var detail string
	var ok bool
	switch req.Protocol {
	case "tcp":
		ok, detail = tcp(ctx, target)
	case "http":
		ok, detail = httpProbe(ctx, target, req)
	case "udp":
		ok, detail = udp(ctx, target, req)
	default:
		return Result{Detail: "protocol must be tcp, http or udp"}
	}
	return Result{OK: ok, Latency: time.Since(start), Detail: detail}
}

func tcp(ctx context.Context, target string) (bool, string) {
	var d net.Dialer
	c, err := d.DialContext(ctx, "tcp", target)
	if err != nil {
		return false, "connect failed: " + short(err)
	}
	_ = c.Close()
	return true, "connected"
}

func httpProbe(ctx context.Context, target string, req Request) (bool, string) {
	method := req.HTTPMethod
	if method == "" {
		method = http.MethodGet
	}
	if method != "GET" && method != "HEAD" && method != "OPTIONS" {
		return false, "method must be GET, HEAD or OPTIONS"
	}
	path := req.HTTPPath
	if path == "" {
		path = "/"
	}
	if !pathOK.MatchString(path) || (req.HTTPHost != "" && !hostOK.MatchString(req.HTTPHost)) {
		return false, "invalid path or host"
	}
	r, err := http.NewRequestWithContext(ctx, method, "http://"+target+path, nil)
	if err != nil {
		return false, "invalid request"
	}
	if req.HTTPHost != "" {
		r.Host = req.HTTPHost
	}
	client := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, err := client.Do(r)
	if err != nil {
		return false, "request failed: " + short(err)
	}
	defer resp.Body.Close()
	good := resp.StatusCode >= 200 && resp.StatusCode < 400
	if len(req.ExpectStatus) > 0 {
		good = false
		for _, w := range req.ExpectStatus {
			if uint32(resp.StatusCode) == w {
				good = true
			}
		}
	}
	return good, fmt.Sprintf("HTTP %d", resp.StatusCode)
}

func udp(ctx context.Context, target string, req Request) (bool, string) {
	var d net.Dialer
	c, err := d.DialContext(ctx, "udp", target)
	if err != nil {
		return false, "dial failed: " + short(err)
	}
	defer c.Close()
	if dl, ok := ctx.Deadline(); ok {
		_ = c.SetDeadline(dl)
	}
	if _, err := c.Write(req.UDPPayload); err != nil {
		return false, "send failed: " + short(err)
	}
	buf := make([]byte, 2048)
	n, err := c.Read(buf)
	if err != nil {
		return false, "no reply: " + short(err)
	}
	if len(req.UDPExpect) > 0 && !bytes.Contains(buf[:n], req.UDPExpect) {
		return false, fmt.Sprintf("reply of %d bytes does not contain the expected bytes", n)
	}
	return true, fmt.Sprintf("reply of %d bytes", n)
}

func short(err error) string {
	s := err.Error()
	if len(s) > 200 {
		s = s[:200]
	}
	return s
}
