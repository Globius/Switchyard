package watchdog

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type rec struct{ calls []string }

func (r *rec) Run(_ context.Context, name string, args ...string) ([]byte, error) {
	r.calls = append(r.calls, name+" "+strings.Join(args, " | "))
	return nil, nil
}

func vips(t *testing.T, content string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "vips.json")
	if err := os.WriteFile(p, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return p
}

func TestWithdrawsAfterThresholdAndReannouncesOnRecovery(t *testing.T) {
	healthy := true
	r := &rec{}
	w := &Watchdog{
		Runner: r, Threshold: 3,
		VIPsFile: vips(t, `{"asn":65010,"prefixes":["192.0.2.20/32","192.0.2.21/32"]}`),
		Check:    func(context.Context) bool { return healthy },
	}
	ctx := context.Background()
	w.Tick(ctx)
	healthy = false
	w.Tick(ctx)
	w.Tick(ctx)
	if len(r.calls) != 0 {
		t.Fatalf("two failures must not withdraw: %v", r.calls)
	}
	w.Tick(ctx)
	if _, withdrawn := w.State(); !withdrawn || len(r.calls) != 2 {
		t.Fatalf("expected two withdrawals, got %v", r.calls)
	}
	if !strings.Contains(r.calls[0], "no network 192.0.2.20/32") || !strings.Contains(r.calls[0], "router bgp 65010") {
		t.Fatalf("unexpected command %q", r.calls[0])
	}
	w.Tick(ctx) // still unhealthy: no repeated withdrawals
	if len(r.calls) != 2 {
		t.Fatalf("withdrawals must not repeat: %v", r.calls)
	}
	healthy = true
	w.Tick(ctx)
	if _, withdrawn := w.State(); withdrawn || len(r.calls) != 4 {
		t.Fatalf("expected re-announcement: %v", r.calls)
	}
	if strings.Contains(r.calls[2], "no network") || !strings.Contains(r.calls[2], "network 192.0.2.20/32") {
		t.Fatalf("unexpected command %q", r.calls[2])
	}
}

func TestAFlappingCheckNeverReachesTheThreshold(t *testing.T) {
	n := 0
	r := &rec{}
	w := &Watchdog{
		Runner: r, Threshold: 3, VIPsFile: vips(t, `{"asn":1,"prefixes":["192.0.2.1/32"]}`),
		Check: func(context.Context) bool { n++; return n%3 == 0 },
	}
	for i := 0; i < 30; i++ {
		w.Tick(context.Background())
	}
	if len(r.calls) != 0 {
		t.Fatalf("flapping must not withdraw: %v", r.calls)
	}
}

func TestNoVIPsFileMeansVrrpModeAndNothingToWithdraw(t *testing.T) {
	r := &rec{}
	w := &Watchdog{Runner: r, Threshold: 1, VIPsFile: filepath.Join(t.TempDir(), "missing.json"),
		Check: func(context.Context) bool { return false }}
	for i := 0; i < 5; i++ {
		if err := w.Tick(context.Background()); err != nil {
			t.Fatal(err)
		}
	}
	if len(r.calls) != 0 {
		t.Fatal("no commands expected")
	}
	if healthy, _ := w.State(); healthy {
		t.Fatal("the unhealthy result must still be reported")
	}
}

func TestInvalidPrefixesAreRefused(t *testing.T) {
	for _, content := range []string{
		`{"asn":1,"prefixes":["192.0.2.1/32; reboot"]}`, `{"asn":0,"prefixes":[]}`, `not json`,
	} {
		r := &rec{}
		w := &Watchdog{Runner: r, Threshold: 1, VIPsFile: vips(t, content),
			Check: func(context.Context) bool { return false }}
		w.Tick(context.Background())
		if len(r.calls) != 0 {
			t.Errorf("%s: commands must not run: %v", content, r.calls)
		}
	}
}

func TestUDPPrefixesFollowIPVSNotHAProxy(t *testing.T) {
	haproxy := false
	udpBad := map[string]bool{}
	r := &rec{}
	w := &Watchdog{
		Runner: r, Threshold: 1,
		VIPsFile: vips(t, `{"asn":65010,"prefixes":["192.0.2.20/32","192.0.2.22/32"],`+
			`"udp_prefixes":["192.0.2.22/32"],"udp_only_prefixes":["192.0.2.22/32"]}`),
		Check:        func(context.Context) bool { return haproxy },
		UDPUnhealthy: func(context.Context) map[string]bool { return udpBad },
	}
	ctx := context.Background()
	w.Tick(ctx)
	if len(r.calls) != 1 || !strings.Contains(r.calls[0], "no network 192.0.2.20/32") {
		t.Fatalf("a failing HAProxy withdraws only the non-UDP VIP: %v", r.calls)
	}
	haproxy = true
	udpBad["192.0.2.22"] = true
	w.Tick(ctx)
	if len(r.calls) != 3 || !strings.Contains(r.calls[1], "network 192.0.2.20/32") ||
		strings.Contains(r.calls[1], "no network") ||
		!strings.Contains(r.calls[2], "no network 192.0.2.22/32") {
		t.Fatalf("HAProxy recovers and the UDP VIP with no healthy real server goes: %v", r.calls)
	}
	udpBad = map[string]bool{}
	w.Tick(ctx)
	if len(r.calls) != 4 || strings.Contains(r.calls[3], "no network") {
		t.Fatalf("the UDP VIP is announced again: %v", r.calls)
	}
	if _, withdrawn := w.State(); withdrawn {
		t.Fatal("nothing should be withdrawn now")
	}
}
