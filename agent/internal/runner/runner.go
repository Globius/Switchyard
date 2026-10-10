// Package runner is the only way the agent starts processes. It runs a fixed allowlist of
// binaries without a shell, so there is no way to execute arbitrary commands through it.
package runner

import (
	"context"
	"fmt"
	"os/exec"
	"time"
)

// Runner runs an allowlisted command and returns its combined output.
type Runner interface {
	Run(ctx context.Context, name string, args ...string) ([]byte, error)
}

// Exec is the real Runner.
type Exec struct {
	// Allowed lists the binaries that may be started (for example "systemctl", "vtysh").
	Allowed map[string]bool
	Timeout time.Duration
}

// Run starts name with args if name is allowlisted.
func (e Exec) Run(ctx context.Context, name string, args ...string) ([]byte, error) {
	if !e.Allowed[name] {
		return nil, fmt.Errorf("command %q is not allowed", name)
	}
	timeout := e.Timeout
	if timeout == 0 {
		timeout = 20 * time.Second
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	return exec.CommandContext(ctx, name, args...).CombinedOutput() // #nosec G204 - allowlisted
}

// Default returns a Runner that may start systemctl and vtysh only.
func Default() Exec {
	return Exec{Allowed: map[string]bool{"systemctl": true, "vtysh": true}}
}
