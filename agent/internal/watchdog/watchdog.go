// Package watchdog withdraws BGP prefixes while the local HAProxy is unhealthy and announces them
// again when it recovers. It never waits on the controller: failover is local.
package watchdog

import (
	"context"
	"encoding/json"
	"fmt"
	"net/netip"
	"os"
	"sync"

	"switchyard/agent/internal/runner"
)

// VIPs is the content of frr/vips.json, delivered in the signed bundle.
type VIPs struct {
	ASN      uint32   `json:"asn"`
	Prefixes []string `json:"prefixes"`
}

// Watchdog is the route health injection state machine.
type Watchdog struct {
	Runner    runner.Runner
	VIPsFile  string
	Check     func(ctx context.Context) bool
	Threshold int // consecutive failures before withdrawing

	mu        sync.Mutex
	failures  int
	healthy   bool
	withdrawn bool
}

// State returns the last health result and whether prefixes are withdrawn.
func (w *Watchdog) State() (healthy, withdrawn bool) {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.healthy, w.withdrawn
}

func (w *Watchdog) load() (*VIPs, error) {
	data, err := os.ReadFile(w.VIPsFile)
	if err != nil {
		return nil, err
	}
	var v VIPs
	if err := json.Unmarshal(data, &v); err != nil {
		return nil, err
	}
	if v.ASN == 0 {
		return nil, fmt.Errorf("vips.json: asn missing")
	}
	for _, p := range v.Prefixes {
		if _, err := netip.ParsePrefix(p); err != nil {
			return nil, fmt.Errorf("vips.json: %q is not a prefix", p)
		}
	}
	return &v, nil
}

func (w *Watchdog) apply(ctx context.Context, v *VIPs, verb string) error {
	for _, p := range v.Prefixes {
		args := []string{
			"-c", "configure terminal",
			"-c", fmt.Sprintf("router bgp %d", v.ASN),
			"-c", "address-family ipv4 unicast",
			"-c", verb + "network " + p,
		}
		if _, err := w.Runner.Run(ctx, "vtysh", args...); err != nil {
			return err
		}
	}
	return nil
}

// Tick runs one health check and changes route announcements if the state requires it.
func (w *Watchdog) Tick(ctx context.Context) error {
	ok := w.Check(ctx)
	w.mu.Lock()
	defer w.mu.Unlock()
	w.healthy = ok
	vips, err := w.load()
	if err != nil {
		// No BGP configuration (VRRP mode) or an unreadable file: nothing to withdraw.
		w.failures = 0
		return nil
	}
	if ok {
		w.failures = 0
		if w.withdrawn {
			if err := w.apply(ctx, vips, ""); err != nil {
				return err
			}
			w.withdrawn = false
		}
		return nil
	}
	w.failures++
	if w.failures >= w.Threshold && !w.withdrawn {
		if err := w.apply(ctx, vips, "no "); err != nil {
			return err
		}
		w.withdrawn = true
	}
	return nil
}
