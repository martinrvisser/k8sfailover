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

class TestHAProxyEdgeCases:
    @patch("failover_manager.socket.socket")
    def test_haproxy_cmd_socket_timeout(self, mock_socket):
        mock_sock_instance = MagicMock()
        mock_socket.return_value.__enter__.return_value = mock_sock_instance
        mock_sock_instance.connect.side_effect = socket.timeout("Connection timed out")

        with pytest.raises(socket.timeout):
            failover_manager.haproxy_cmd("show info")

    @patch("failover_manager.haproxy_cmd")
    @patch("failover_manager.log")
    def test_haproxy_servers_empty_response(self, mock_log, mock_cmd):
        # Simulate HAProxy returning a completely empty string
        mock_cmd.return_value = ""
        
        servers = failover_manager.haproxy_servers()
        
        assert servers == []
        mock_log.warning.assert_called_once()
        assert "returned no servers" in mock_log.warning.call_args[0][0]

    @patch("failover_manager.haproxy_cmd")
    @patch("failover_manager.log")
    def test_haproxy_servers_malformed_response(self, mock_log, mock_cmd):
        # Simulate HAProxy returning headers but no server rows
        mock_cmd.return_value = "# be_id be_name srv_id srv_name srv_addr\n"
        
        servers = failover_manager.haproxy_servers()
        
        assert servers == []
        mock_log.warning.assert_called_once()

class TestSetPrimaryEdgeCases:
    @patch("failover_manager.get_server_name")
    @patch("failover_manager.haproxy_cmd")
    @patch("failover_manager.log")
    def test_set_primary_unresolved_server(self, mock_log, mock_cmd, mock_get_name):
        # Simulate get_server_name failing to find a match
        mock_get_name.return_value = None
        
        failover_manager.set_primary("10.99.99.99", "6379")
        
        # haproxy_cmd should NOT be called if the server name wasn't found
        mock_cmd.assert_not_called()
        mock_log.warning.assert_called_once()
        assert "Could not resolve server name" in mock_log.warning.call_args[0][0]

    @patch("failover_manager.get_server_name")
    @patch("failover_manager.log")
    def test_set_primary_oserror(self, mock_log, mock_get_name):
        # Simulate a socket error when talking to HAProxy to resolve the name
        mock_get_name.side_effect = OSError("Socket closed")
        
        failover_manager.set_primary("10.0.0.1", "6379")
        
        mock_log.warning.assert_called_once()
        assert "HAProxy socket error while resolving" in mock_log.warning.call_args[0][0]

class TestSentinelEdgeCases:
    @patch("failover_manager.set_primary")
    @patch("failover_manager.log")
    def test_reconcile_exception(self, mock_log, mock_set_primary):
        mock_redis = MagicMock()
        # Simulate Sentinel throwing a ConnectionError during reconcile
        mock_redis.sentinel_get_master_addr_by_name.side_effect = redis.ConnectionError("Sentinel unreachable")
        
        failover_manager.reconcile(mock_redis)
        
        # Primary should not be updated
        mock_set_primary.assert_not_called()
        mock_log.warning.assert_called_once()
        assert "Could not query current primary" in mock_log.warning.call_args[0][0]

    @patch("failover_manager.wait_for_haproxy_socket")
    @patch("failover_manager.connect_sentinel")
    @patch("failover_manager.reconcile")
    @patch("failover_manager.set_primary")
    @patch("failover_manager.log")
    def test_main_malformed_switch_master_event(self, mock_log, mock_set_primary, mock_reconcile, mock_connect, mock_wait):
        mock_redis = MagicMock()
        mock_pubsub = MagicMock()
        mock_connect.return_value = mock_redis
        mock_redis.pubsub.return_value = mock_pubsub
        
        # Simulate a +switch-master event that is missing the port fields (malformed)
        mock_pubsub.get_message.side_effect = [
            {"type": "message", "channel": "+switch-master", "data": "mymaster 10.0.0.1"},
            KeyboardInterrupt("Break loop")
        ]
        
        with pytest.raises(KeyboardInterrupt):
            failover_manager.main()
            
        # The script should catch the malformed data and NOT call set_primary
        mock_set_primary.assert_not_called()
        
    @patch("failover_manager.wait_for_haproxy_socket")
    @patch("failover_manager.connect_sentinel")
    @patch("failover_manager.reconcile")
    @patch("failover_manager.time.sleep")
    def test_main_pubsub_connection_drop(self, mock_sleep, mock_reconcile, mock_connect, mock_wait):
        mock_redis = MagicMock()
        mock_pubsub = MagicMock()
        mock_connect.return_value = mock_redis
        mock_redis.pubsub.return_value = mock_pubsub
        
        # Simulate pubsub throwing an unexpected socket error, forcing a reconnect
        mock_pubsub.subscribe.side_effect = [
            redis.ConnectionError("Socket closed remotely"),
            KeyboardInterrupt("Break loop on second attempt")
        ]
        
        with pytest.raises(KeyboardInterrupt):
            failover_manager.main()
            
        # The script should have attempted to sleep and reconnect
        mock_sleep.assert_called_with(2)
        mock_pubsub.close.assert_called_once()
