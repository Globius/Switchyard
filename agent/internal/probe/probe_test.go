package probe

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"
)

func hostPort(t *testing.T, addr string) (string, uint32) {
	t.Helper()
	h, p, err := net.SplitHostPort(addr)
	if err != nil {
		t.Fatal(err)
	}
	n, _ := strconv.Atoi(p)
	return h, uint32(n)
}

func TestMembersAreReadFromTheAppliedConfig(t *testing.T) {
	dir := t.TempDir()
	cfg := filepath.Join(dir, "haproxy.cfg")
	ka := filepath.Join(dir, "keepalived.conf")
	os.WriteFile(cfg, []byte("backend b\n    server web-1 10.0.0.5:8080 check\n    server v6 ipv6@2001:db8::5:80\n    server np 10.0.0.7 check port 9000\n"), 0o600)
	os.WriteFile(ka, []byte("virtual_server 192.0.2.1 53 {\n  real_server 10.0.0.9 53 {\n  }\n}\n"), 0o600)
	got := Members(cfg, ka)
	for _, want := range []string{"10.0.0.5:8080", "10.0.0.9:53", "10.0.0.7:9000", "[2001:db8::5]:80"} {
		if !got[want] {
			t.Errorf("missing %s in %v", want, got)
		}
	}
	if len(got) < 2 {
		t.Errorf("unexpected members %v", got)
	}
}

func TestAnUnconfiguredTargetIsRefused(t *testing.T) {
	res := Run(context.Background(), Request{Protocol: "tcp", Address: "10.0.0.1", Port: 22, Timeout: time.Second}, map[string]bool{})
	if !res.Refused || res.OK {
		t.Fatalf("expected refusal, got %+v", res)
	}
}

func TestHTTPProbe(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" {
			w.WriteHeader(200)
			return
		}
		w.WriteHeader(503)
	}))
	defer srv.Close()
	host, port := hostPort(t, srv.Listener.Addr().String())
	allowed := map[string]bool{net.JoinHostPort(host, strconv.Itoa(int(port))): true}
	ok := Run(context.Background(), Request{Protocol: "http", Address: host, Port: port, Timeout: time.Second, HTTPPath: "/healthz"}, allowed)
	if !ok.OK || ok.Detail != "HTTP 200" {
		t.Fatalf("got %+v", ok)
	}
	bad := Run(context.Background(), Request{Protocol: "http", Address: host, Port: port, Timeout: time.Second, HTTPPath: "/x"}, allowed)
	if bad.OK || bad.Detail != "HTTP 503" {
		t.Fatalf("got %+v", bad)
	}
	want := Run(context.Background(), Request{Protocol: "http", Address: host, Port: port, Timeout: time.Second, HTTPPath: "/x", ExpectStatus: []uint32{503}}, allowed)
	if !want.OK {
		t.Fatalf("expected 503 to be accepted: %+v", want)
	}
	evil := Run(context.Background(), Request{Protocol: "http", Address: host, Port: port, Timeout: time.Second, HTTPPath: "/a b\r\nX: y"}, allowed)
	if evil.OK {
		t.Fatalf("unsafe path accepted")
	}
}

func TestTCPAndUDPProbes(t *testing.T) {
	l, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer l.Close()
	go func() {
		for {
			c, err := l.Accept()
			if err != nil {
				return
			}
			c.Close()
		}
	}()
	host, port := hostPort(t, l.Addr().String())
	allowed := map[string]bool{net.JoinHostPort(host, strconv.Itoa(int(port))): true}
	if r := Run(context.Background(), Request{Protocol: "tcp", Address: host, Port: port, Timeout: time.Second}, allowed); !r.OK {
		t.Fatalf("tcp: %+v", r)
	}
	pc, err := net.ListenPacket("udp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer pc.Close()
	go func() {
		buf := make([]byte, 64)
		n, addr, err := pc.ReadFrom(buf)
		if err == nil {
			pc.WriteTo(append([]byte("pong:"), buf[:n]...), addr)
		}
	}()
	uh, up := hostPort(t, pc.LocalAddr().String())
	ua := map[string]bool{net.JoinHostPort(uh, strconv.Itoa(int(up))): true}
	r := Run(context.Background(), Request{Protocol: "udp", Address: uh, Port: up, Timeout: time.Second, UDPPayload: []byte("ping"), UDPExpect: []byte("pong")}, ua)
	if !r.OK {
		t.Fatalf("udp: %+v", r)
	}
	if bad := Run(context.Background(), Request{Protocol: "tcp", Address: host, Port: port, Timeout: time.Millisecond}, allowed); bad.OK || bad.Refused {
		t.Fatalf("short timeout should fail validation: %+v", bad)
	}
}
