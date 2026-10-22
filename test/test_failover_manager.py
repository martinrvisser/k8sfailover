import pytest
from unittest.mock import patch, MagicMock, call
import socket
import redis

# Import the script being tested
import failover_manager

@pytest.fixture
def mock_sleep():
    with patch("failover_manager.time.sleep") as mock:
        yield mock

class TestHAProxyCommands:
    @patch("failover_manager.socket.socket")
    def test_haproxy_cmd_success(self, mock_socket):
        mock_sock_instance = MagicMock()
        mock_socket.return_value.__enter__.return_value = mock_sock_instance
        mock_sock_instance.recv.side_effect = [b"mock_response\n", b""]

        result = failover_manager.haproxy_cmd("show servers state")
        
        mock_sock_instance.connect.assert_called_once_with(failover_manager.HAPROXY_SOCK)
        mock_sock_instance.sendall.assert_called_once_with(b"show servers state\n")
        assert result == "mock_response"

    @patch("failover_manager.haproxy_cmd")
    def test_haproxy_servers_parsing(self, mock_cmd):
        mock_cmd.return_value = """1
# be_id be_name srv_id srv_name srv_addr srv_op_state srv_port srv_fqdn
3 valkey_primary 1 valkey-0 10.0.0.1 2 6379 valkey-0.valkey.svc
3 valkey_primary 2 valkey-1 10.0.0.2 2 6379 valkey-1.valkey.svc
"""
        servers = failover_manager.haproxy_servers()
        assert len(servers) == 2
        assert servers[0]["srv_name"] == "valkey-0"
        assert servers[0]["srv_addr"] == "10.0.0.1"
        assert servers[1]["srv_fqdn"] == "valkey-1.valkey.svc"

class TestServerResolution:
    @pytest.fixture
    def mock_haproxy_servers(self):
        with patch("failover_manager.haproxy_servers") as mock:
            mock.return_value = [
                {"srv_name": "valkey-0", "srv_addr": "10.0.0.1", "srv_port": "6379", "srv_fqdn": "valkey-0.local"},
                {"srv_name": "valkey-1", "srv_addr": "10.0.0.2", "srv_port": "6379", "srv_fqdn": "-"},
            ]
            yield mock

    def test_get_server_name_fqdn_match(self, mock_haproxy_servers):
        assert failover_manager.get_server_name("valkey-0.local", "6379") == "valkey-0"

    def test_get_server_name_name_match(self, mock_haproxy_servers):
        assert failover_manager.get_server_name("valkey-1", "6379") == "valkey-1"

    def test_get_server_name_ip_match(self, mock_haproxy_servers):
        assert failover_manager.get_server_name("10.0.0.2", "6379") == "valkey-1"

    @patch("failover_manager.socket.getaddrinfo")
    def test_get_server_name_dns_resolution(self, mock_getaddrinfo, mock_haproxy_servers):
        # Simulate Sentinel returning an IP, but HAProxy only knows the FQDN which resolves to that IP
        mock_getaddrinfo.side_effect = lambda host, port: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('10.0.0.1', port))
        ] if host == "valkey-0.local" else []
        
        assert failover_manager.get_server_name("10.0.0.1", "6379") == "valkey-0"

    @patch("failover_manager.get_server_name")
    @patch("failover_manager.haproxy_cmd")
    def test_set_primary(self, mock_cmd, mock_get_name):
        mock_get_name.return_value = "valkey-1"
        failover_manager.set_primary("10.0.0.2", "6379")
        
        mock_cmd.assert_called_once_with(f"enable server {failover_manager.BACKEND_NAME}/valkey-1")

class TestSentinelConnection:
    @patch("failover_manager.redis.Redis")
    def test_connect_sentinel_success(self, mock_redis):
        mock_instance = MagicMock()
        mock_redis.return_value = mock_instance
        
        r = failover_manager.connect_sentinel()
        mock_instance.ping.assert_called_once()
        assert r == mock_instance

    @patch("failover_manager.redis.Redis")
    def test_connect_sentinel_auth_retry(self, mock_redis, mock_sleep):
        mock_instance = MagicMock()
        # Fail first with Auth error, succeed second time
        mock_instance.ping.side_effect = [redis.AuthenticationError("NOAUTH"), None]
        mock_redis.return_value = mock_instance
        
        r = failover_manager.connect_sentinel()
        assert mock_instance.ping.call_count == 2
        mock_sleep.assert_called_once_with(5)

class TestEventLoop:
    @patch("failover_manager.set_primary")
    def test_reconcile(self, mock_set_primary):
        mock_redis = MagicMock()
        mock_redis.sentinel_get_master_addr_by_name.return_value = ("10.0.0.5", 6379)
        
        failover_manager.reconcile(mock_redis)
        mock_redis.sentinel_get_master_addr_by_name.assert_called_once_with(failover_manager.MASTER_NAME)
        mock_set_primary.assert_called_once_with("10.0.0.5", "6379")

    @patch("failover_manager.wait_for_haproxy_socket")
    @patch("failover_manager.connect_sentinel")
    @patch("failover_manager.reconcile")
    @patch("failover_manager.set_primary")
    def test_main_switch_master_event(self, mock_set_primary, mock_reconcile, mock_connect, mock_wait):
        mock_redis = MagicMock()
        mock_pubsub = MagicMock()

        mock_connect.return_value = mock_redis
        mock_redis.pubsub.return_value = mock_pubsub

        # Simulate one +switch-master event, then throw KeyboardInterrupt
        # to break the infinite loop (escapes the `except Exception:` block).
        mock_pubsub.get_message.side_effect = [
            {"type": "message", "channel": "+switch-master", "data": "mymaster 10.0.0.1 6379 10.0.0.2 6379"},
            KeyboardInterrupt("Break loop")
        ]

        with pytest.raises(KeyboardInterrupt, match="Break loop"):
            failover_manager.main()

        mock_pubsub.subscribe.assert_called_once_with("+switch-master")
        mock_reconcile.assert_called_once_with(mock_redis)
        mock_set_primary.assert_called_once_with("10.0.0.2", "6379")
