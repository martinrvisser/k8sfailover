FROM debian:12

RUN apt update && apt install -y python3 python3-redis
WORKDIR /opt
COPY failover-manager.py /opt/failover-manager.py
