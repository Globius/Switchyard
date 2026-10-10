package runner

import (
	"context"
	"testing"
)

func TestOnlyAllowlistedBinariesRun(t *testing.T) {
	r := Default()
	for _, name := range []string{"sh", "bash", "/bin/sh", "rm", "curl", "systemctl ", "../systemctl"} {
		if _, err := r.Run(context.Background(), name, "-c", "id"); err == nil {
			t.Errorf("%q must be refused", name)
		}
	}
}
