# Deploying on AWS

Two routes. Route 1 gets a public demo running in about 15 minutes. Route 2
is the production-shaped setup with managed Postgres and more than one instance.

## Route 1: one EC2 instance with Docker Compose

Everything (Postgres and two API instances) runs on one machine. Good for a
demo; for durable data use RDS (Route 2).

1. **Launch an instance.** EC2 → Launch instance → *Amazon Linux 2023*, a
   free-tier eligible type (`t3.micro` / `t2.micro`), and a key pair.
2. **Security group inbound rules:**
   - SSH `22` from *My IP* only
   - Custom TCP `8000-8001` from `0.0.0.0/0` (the two API instances)

   Do **not** open `5432`.
3. **Install Docker and run the stack:**

   ```bash
   ssh -i your-key.pem ec2-user@<public-ip>
   sudo dnf install -y docker git
   sudo systemctl enable --now docker
   sudo usermod -aG docker ec2-user && newgrp docker
   sudo mkdir -p /usr/local/lib/docker/cli-plugins
   sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m) \
        -o /usr/local/lib/docker/cli-plugins/docker-compose
   sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

   git clone https://github.com/yashrajkanawade357/Real-Time-Bidding-backend.git
   cd Real-Time-Bidding-backend
   docker compose up -d --build
   ```

4. Open `http://<public-ip>:8000` and `http://<public-ip>:8001` in two tabs.

Before sharing the URL publicly, set `ENABLE_UNSAFE_DEMO: "false"` in
`docker-compose.yml` unless you want the race demo reachable.

## Route 2: ECS Fargate + Application Load Balancer + RDS PostgreSQL

```
Internet ─▶ ALB (HTTP/HTTPS, WebSocket upgrade) ─▶ ECS service: 2+ Fargate tasks ─▶ RDS PostgreSQL
                                                     (each task LISTENs on auction_events)
```

### 1. Database: RDS for PostgreSQL

- Engine PostgreSQL 16 or 17, `db.t4g.micro`, private subnets, no public access.
- Security group: allow `5432` **only from the ECS tasks' security group**.
- Store the connection string in **Secrets Manager** as
  `postgresql://<user>:<password>@<endpoint>:5432/bidding` (create the
  `bidding` database once, or set it as the initial database name).

Nothing else is needed. The app runs its migrations on startup, guarded by an
advisory lock so parallel tasks don't collide.

### 2. Image: ECR

```bash
aws ecr create-repository --repository-name realtime-bidding
aws ecr get-login-password | docker login --username AWS --password-stdin <account>.dkr.ecr.<region>.amazonaws.com
docker build -t realtime-bidding .
docker tag realtime-bidding <account>.dkr.ecr.<region>.amazonaws.com/realtime-bidding:latest
docker push <account>.dkr.ecr.<region>.amazonaws.com/realtime-bidding:latest
```

### 3. Service: ECS on Fargate

- Task definition: the image above, container port `8000`, 0.25 vCPU / 0.5 GB.
- Environment: `DATABASE_URL` from the Secrets Manager secret,
  `ENABLE_UNSAFE_DEMO=false`, and `DB_POOL_MAX` sized so that
  `tasks × DB_POOL_MAX + tasks` (one listener connection each) stays under
  RDS's `max_connections`.
- Service: **desired count 2**, in private subnets, attached to the target
  group below.

### 4. Load balancer: ALB

- Target group: protocol HTTP, port `8000`, target type `ip`, health check
  path `/health`.
- Listener: HTTP `80` (and HTTPS `443` with an ACM certificate, which gives
  the browser `wss://` automatically).
- **WebSockets:** the ALB supports the upgrade natively; no extra settings.
- **Idle timeout:** the default 60 s is fine. uvicorn pings every socket every
  20 s, so an idle connection is never cut.
- **Stickiness: leave it off.** A client can reconnect to any task, because
  state lives in RDS and every task receives every event over LISTEN/NOTIFY.

### 5. Verify

```bash
# against the ALB DNS name
python scripts/reconnect_demo.py --base-url http://<alb-dns-name>
```

Open the site in two browsers. With two tasks running, the ALB spreads them
across tasks, yet each still sees the other's bids live. That is the
LISTEN/NOTIFY fan-out working across instances.

## Notes

- **Connection proxies (RDS Proxy, PgBouncer in transaction mode):** fine for
  the bid pool, but the `LISTEN` connection needs a long-lived session of its
  own, so point it at the database endpoint directly.
- **Observability:** FastAPI has built-in OpenTelemetry support. Install
  `fastapi[opentelemetry]` and set `OTEL_EXPORTER_OTLP_ENDPOINT` to an ADOT
  collector, and request traces and metrics land in CloudWatch / X-Ray. Uvicorn logs go to CloudWatch Logs via the
  `awslogs` driver.
- **Cost:** stop or delete the RDS instance, ECS service and ALB when you're
  done. The ALB and RDS bill hourly even when idle.
