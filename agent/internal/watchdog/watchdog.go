// Package watchdog withdraws BGP prefixes while the service behind them is unhealthy and announces
// them again when it recovers: the local HAProxy for HTTP and TCP VIPs, keepalived and its IPVS
// real servers for UDP VIPs. It never waits on the controller: failover is local.
package watchdog

import (
	"context"
	"encoding/json"
	"fmt"
	"net/netip"
	"os"
	"sort"
	"sync"

	"switchyard/agent/internal/runner"
)

// VIPs is the content of frr/vips.json, delivered in the signed bundle.
type VIPs struct {
	ASN      uint32   `json:"asn"`
	Prefixes []string `json:"prefixes"`
	// UDPPrefixes are the prefixes served by IPVS; they also need keepalived and a healthy real server.
	UDPPrefixes []string `json:"udp_prefixes"`
	// UDPOnlyPrefixes are the UDP prefixes no HAProxy service uses; HAProxy health does not
	// affect them.
	UDPOnlyPrefixes []string `json:"udp_only_prefixes"`
}

// Watchdog is the route health injection state machine.
type Watchdog struct {
	Runner    runner.Runner
	VIPsFile  string
	Check     func(ctx context.Context) bool
	Threshold int // consecutive failures before withdrawing
	// UDPUnhealthy returns the VIP addresses (without prefix length) whose UDP services cannot
	// serve. Optional; without it UDP prefixes follow the HAProxy check like any other.
	UDPUnhealthy func(ctx context.Context) map[string]bool

	mu        sync.Mutex
	failures  map[string]int
	healthy   bool
	withdrawn map[string]bool
}

// State returns the last HAProxy health result and whether any prefix is withdrawn.
func (w *Watchdog) State() (healthy, withdrawn bool) {
	w.mu.Lock()
	defer w.mu.Unlock()
	return w.healthy, len(w.withdrawn) > 0
}

// Withdrawn returns the withdrawn prefixes, sorted.
func (w *Watchdog) Withdrawn() []string {
	w.mu.Lock()
	defer w.mu.Unlock()
	out := make([]string, 0, len(w.withdrawn))
	for p := range w.withdrawn {
		out = append(out, p)
	}
	sort.Strings(out)
	return out
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

func (w *Watchdog) apply(ctx context.Context, v *VIPs, prefix, verb string) error {
	args := []string{
		"-c", "configure terminal",
		"-c", fmt.Sprintf("router bgp %d", v.ASN),
		"-c", "address-family ipv4 unicast",
		"-c", verb + "network " + prefix,
	}
	_, err := w.Runner.Run(ctx, "vtysh", args...)
	return err
}

func set(list []string) map[string]bool {
	out := map[string]bool{}
	for _, p := range list {
		out[p] = true
	}
	return out
}

// Tick runs one health check and changes route announcements if the state requires it.
func (w *Watchdog) Tick(ctx context.Context) error {
	ok := w.Check(ctx)
	w.mu.Lock()
	defer w.mu.Unlock()
	w.healthy = ok
	if w.failures == nil {
		w.failures, w.withdrawn = map[string]int{}, map[string]bool{}
	}
	vips, err := w.load()
	if err != nil {
		// No BGP configuration (VRRP mode) or an unreadable file: nothing to withdraw.
		w.failures = map[string]int{}
		return nil
	}
	udp, udpOnly := set(vips.UDPPrefixes), set(vips.UDPOnlyPrefixes)
	var udpBad map[string]bool
	if w.UDPUnhealthy != nil && len(udp) > 0 {
		udpBad = w.UDPUnhealthy(ctx)
	}
	for _, p := range vips.Prefixes {
		healthy := ok || udpOnly[p]
		if prefix, perr := netip.ParsePrefix(p); perr == nil && udp[p] && udpBad[prefix.Addr().String()] {
			healthy = false
		}
		if healthy {
			w.failures[p] = 0
			if w.withdrawn[p] {
				if err := w.apply(ctx, vips, p, ""); err != nil {
					return err
				}
				delete(w.withdrawn, p)
			}
			continue
		}
		w.failures[p]++
		if w.failures[p] >= w.Threshold && !w.withdrawn[p] {
			if err := w.apply(ctx, vips, p, "no "); err != nil {
				return err
			}
			w.withdrawn[p] = true
		}
	}
	return nil
}
