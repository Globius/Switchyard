// Package server implements the Agent gRPC service.
package server

import (
	"context"
	"time"

	"google.golang.org/protobuf/proto"

	pb "switchyard/agent/gen/switchyard/agent/v1"
	"switchyard/agent/internal/bundle"
	"switchyard/agent/internal/status"
)

// Server implements pb.AgentServer.
type Server struct {
	pb.UnimplementedAgentServer
	Applier   *bundle.Applier
	Collector *status.Collector
	// Interval is how often Watch re-checks; Heartbeat forces a send even without changes.
	Interval  time.Duration
	Heartbeat time.Duration
}

// ApplyBundle verifies and applies a bundle.
func (s *Server) ApplyBundle(ctx context.Context, req *pb.ApplyBundleRequest) (*pb.ApplyBundleResponse, error) {
	return s.Applier.Apply(ctx, req), nil
}

// GetStatus returns a status snapshot.
func (s *Server) GetStatus(ctx context.Context, _ *pb.GetStatusRequest) (*pb.Status, error) {
	return s.Collector.Collect(ctx), nil
}

func comparable(st *pb.Status) *pb.Status {
	c := proto.Clone(st).(*pb.Status)
	c.ObservedAtUnix = 0
	return c
}

// Watch streams a snapshot when something changes, and at least every Heartbeat.
func (s *Server) Watch(_ *pb.WatchRequest, stream pb.Agent_WatchServer) error {
	interval, heartbeat := s.Interval, s.Heartbeat
	if interval == 0 {
		interval = 5 * time.Second
	}
	if heartbeat == 0 {
		heartbeat = 30 * time.Second
	}
	ctx := stream.Context()
	var last *pb.Status
	lastSent := time.Time{}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		st := s.Collector.Collect(ctx)
		if last == nil || !proto.Equal(comparable(st), comparable(last)) || time.Since(lastSent) >= heartbeat {
			if err := stream.Send(st); err != nil {
				return err
			}
			last, lastSent = st, time.Now()
		}
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		}
	}
}
