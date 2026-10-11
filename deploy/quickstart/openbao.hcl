# Test instance: one-node integrated (raft) storage, no TLS inside the compose network. See docs/production.md for a
# real deployment (TLS, a three-node raft cluster or an external backend, auto-unseal).
ui = true
disable_mlock = true
storage "raft" {
  path    = "/openbao/file"
  node_id = "quickstart"
}
listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = 1
}
api_addr     = "http://openbao:8200"
cluster_addr = "http://127.0.0.1:8201"
