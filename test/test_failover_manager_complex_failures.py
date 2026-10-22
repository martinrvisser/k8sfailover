import pytest
from unittest.mock import patch, MagicMock
import socket
import redis

# Import the script being tested
import failover_manager

@pytest.fixture
def mock_sleep():
    with patch("failover_manager.time.sleep") as mock:
        yield mock

class TestCascadingFailures:
    @patch("failover_manager.set_primary")
    @patch("failover_manager.log")
    def test_reconcile_during_active_election(self, mock_log, mock_set_primary):
        mock_redis = MagicMock()
        # Sentinel returns None for the master address because the election is currently ongoing
        # and no quorum has been reached yet.
        mock_redis.sentinel_get_master_addr_by_name.return_value = None
        
        failover_manager.reconcile(mock_redis)
        
        # The script should handle the None return gracefully and wait for the pub/sub event
        mock_set_primary.assert_not_called()
        # No warning should be logged because returning None is a valid Sentinel state
        mock_log.warning.assert_not_called()

    @patch("failover_manager.haproxy_servers")
    @patch("failover_manager.socket.getaddrinfo")
    def test_dns_resolution_failure_fallback(self, mock_getaddrinfo, mock_haproxy):
        mock_haproxy.return_value = [
            {"srv_name": "valkey-2", "srv_addr": "10.0.0.3", "srv_port": "6379", "srv_fqdn": "valkey-2.local"}
        ]
        # Simulate CoreDNS/kube-dns being completely down
        mock_getaddrinfo.side_effect = socket.gaierror("Name or service not known")
        
        # Even with DNS down, the script should catch the gaierror internally returning an empty set,
        # and successfully fall back to direct IP string matching.
        result = failover_manager.get_server_name("10.0.0.3", "6379")
        
        assert result == "valkey-2"

    @patch("failover_manager.get_server_name")
    @patch("failover_manager.haproxy_cmd")
    @patch("failover_manager.wait_for_haproxy_socket")
    @patch("failover_manager.connect_sentinel")
    @patch("failover_manager.reconcile")
    @patch("failover_manager.time.sleep")
    def test_haproxy_crashes_mid_failover(self, mock_sleep, mock_reconcile, mock_connect, mock_wait, mock_haproxy_cmd, mock_get_server_name):
        mock_redis = MagicMock()
        mock_pubsub = MagicMock()
        mock_connect.return_value = mock_redis
        mock_redis.pubsub.return_value = mock_pubsub
        
        mock_get_server_name.return_value = "valkey-1"
        
        # 1. We receive a valid +switch-master event.
        # 2. We resolve the name successfully.
        # 3. Exactly as we send the "enable server" command to HAProxy, HAProxy crashes and closes the socket.
        mock_pubsub.get_message.side_effect = [
            {"type": "message", "channel": "+switch-master", "data": "mymaster 10.0.0.1 6379 10.0.0.2 6379"},
            KeyboardInterrupt("Break loop on second pass")
        ]
        mock_haproxy_cmd.side_effect = OSError("HAProxy socket closed abruptly")
        
        with pytest.raises(KeyboardInterrupt):
            failover_manager.main()
            
        # The script's main loop should catch the OSError bubbling up from haproxy_cmd,
        # close the pub/sub connection, sleep for 2 seconds, and restart the loop to try again.
        mock_sleep.assert_called_with(2)
        mock_pubsub.close.assert_called_once()

    @patch("failover_manager.set_primary")
    @patch("failover_manager.wait_for_haproxy_socket")
    @patch("failover_manager.connect_sentinel")
    @patch("failover_manager.reconcile")
    def test_rapid_succession_split_brain_failovers(self, mock_reconcile, mock_connect, mock_wait, mock_set_primary):
        mock_redis = MagicMock()
        mock_pubsub = MagicMock()
        mock_connect.return_value = mock_redis
        mock_redis.pubsub.return_value = mock_pubsub
        
        # Simulate a split-brain or rapid cascade where two failovers happen back-to-back 
        # before the script even idles.
        mock_pubsub.get_message.side_effect = [
            {"type": "message", "channel": "+switch-master", "data": "mymaster 10.0.0.1 6379 10.0.0.2 6379"},
            {"type": "message", "channel": "+switch-master", "data": "mymaster 10.0.0.2 6379 10.0.0.3 6379"},
            KeyboardInterrupt("Break loop")
        ]
        
        with pytest.raises(KeyboardInterrupt):
            failover_manager.main()
            
        # Ensure the script processes both events in order without breaking the loop or caching old states.
        assert mock_set_primary.call_count == 2
        mock_set_primary.assert_any_call("10.0.0.2", "6379")
        mock_set_primary.assert_any_call("10.0.0.3", "6379")
