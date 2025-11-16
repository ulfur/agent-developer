# Nightshift CDK App

This directory contains the AWS CDK (v2, Python) project that manages the
Nightshift infrastructure described in `ROADMAP.md` Phase 1.1.

## Layout
- `app.py` – CDK entrypoint that loads per-instance config from
  `instances/<name>.yml` and synthesizes the stack.
- `nightshift_stack.py` – provisions the VPC, storage, and compute resources
  for the selected Nightshift instance.
- `config.py` – helper to read instance definitions and provide CDK
  `Environment` objects.
- `instances/` – YAML config per environment/instance. Copy
  `example-dev.yml` to `instances/<name>.yml` and set the AWS account, region,
  tags, and custom parameters for that deployment.
- `cdk.json` – default CDK CLI configuration (points at `app.py` and picks the
  `example-dev` instance by default).
- `requirements.txt` – pinned Python dependencies for the CDK app.

Synthesis outputs under `cdk.out/` are ignored in git; each operator runs the
commands locally with their own AWS credentials.

## Prerequisites
1. Install Node.js 18+ and `npx`.
2. Install the AWS CDK CLI globally (`npm install -g aws-cdk`).
3. Create/activate a Python 3.11 virtualenv inside `cdk/.venv/`.
4. Install dependencies: `pip install -r requirements.txt`.
5. Copy `instances/example-dev.yml` to `instances/<your-instance>.yml` and fill
   in the AWS account/region plus any stack parameters.

## Common commands
Use the helper script from the repo root:

```bash
scripts/cdk.sh synth -c instance=prod-eu
scripts/cdk.sh diff -c instance=qa-us
scripts/cdk.sh deploy Nightshift-prod-eu -c instance=prod-eu
```

`scripts/cdk.sh` simply cd's into `cdk/` and shells out to `npx cdk`, so any
global CDK CLI options keep working. If you prefer running manually, `cdk.json`
lets you run the same commands directly inside this directory.

## Operator workflow
Because the Pi host intentionally cannot store AWS credentials, operators pull
the latest branch locally and run the CDK commands from their workstation:

1. Set AWS credentials/profile in your shell (for the account referenced in the
   instance config).
2. Run `scripts/cdk.sh bootstrap -c instance=<name>` the first time in each
   account/region.
3. Run `scripts/cdk.sh synth -c instance=<name>` to validate the template.
4. Run `scripts/cdk.sh deploy --all -c instance=<name>` (or target a single
   stack) when changes need to land.

Document every deployment request in a Human Task so operators know when to run
these commands.

> **Note:** CDK performs AWS context lookups (availability zones, etc.). When
> running without configured AWS credentials, pass `--no-lookups` and ensure
> `cdk.context.json` already includes the required keys (the repo seeds an
> example entry for `example-dev` so automated synths can run without creds).

## Container runtime on deployed hosts

Phase 1.2 introduced Docker images for both the backend and the nginx
frontend. Instances created by this stack should follow these steps after the
Auto Scaling group attaches the EFS volume:

1. Mount the shared filesystem at `/workspaces` (the stack’s `workspacePath`
   parameter already defaults to that path). Every container expects to find
   its project checkouts under this directory.
2. Clone this repository into `/workspaces/nightshift` (or set
   `NIGHTSHIFT_REPO_HOST_PATH` in `.env` so Compose points at the correct
   checkout).
3. Copy `docker/.env.example` to `.env`, update the workspace path/ports for
   the instance, and run `./scripts/nightshift_compose.sh up` to build and
   start the containers. The helper script ensures the `/workspaces`
   directory exists before invoking Docker so Pi and cloud hosts share the
   same layout.
4. Use `./scripts/nightshift_compose.sh smoke` before deploying new images to
   catch compose regressions or missing dependencies.

Expose ports 80/443 (Traefik) and, if desired, the legacy `FRONTEND_HTTP_PORT`
so operators can reach the router + nginx frontend. The new `routerCidrIngress`
parameter controls which CIDRs are allowed to hit HTTP/HTTPS on the compute
security group. When the e-ink display is attached to a Pi, map `/dev/gpiomem`
and `/dev/spidev*` into the backend container and set `ENABLE_EINK_DISPLAY=1`
via the `.env` file or the Compose override.

## Stack resources

`NightshiftStack` now creates:

- A dedicated VPC with public + private subnets across at least two AZs, plus a
  managed NAT gateway for private egress.
- Security groups for the compute layer and the shared EFS filesystem.
- An encrypted EFS filesystem + access point that backs `/workspaces/nightshift`.
- An S3 bucket for prompt log exports and archive bundles (with retention rules).
- An EC2 Auto Scaling group (configurable instance type/AMI/disk/min/max) that
  mounts the EFS share and can scale out as needed.

The stack emits CloudFormation outputs for VPC/subnet CIDRs, security group IDs,
the EFS identifiers, bucket name, and Auto Scaling group name so operators can
wire additional services manually.

## Instance parameters

Customize deployments via `instances/<name>.yml`. The `parameters` map accepts:

| Key | Default | Description |
| --- | --- | --- |
| `vpcCidr` | `10.42.0.0/16` | Base CIDR for the dedicated VPC. |
| `publicSubnetMask` / `privateSubnetMask` | `24` / `20` | Netmask for subnets in each AZ. |
| `maxAzs` | `2` | Number of AZs to spread subnets across (must be ≥2). |
| `availabilityZones` | _derived from region_ | Optional explicit AZ list (comma-separated or YAML list) if you need to control which AZ suffixes are used. |
| `natGateways` | `1` | NAT gateway count. |
| `sshCidrIngress` | _required_ | IPv4/IPv6 CIDRs allowed to SSH into the compute nodes (comma-separated or YAML list). |
| `routerCidrIngress` | _optional_ | CIDRs allowed to reach Traefik/HTTP(S). Leave unset to keep the router private. |
| `computeAmiId` | _required_ | AMI used by the compute Auto Scaling group (legacy `codexAmiId` is still accepted). |
| `computeInstanceType` | `t4g.small` | Instance type for the Auto Scaling group. |
| `instanceDiskSizeGiB` | `64` | Size of the root EBS volume attached to each instance. |
| `asgMinCapacity` / `asgDesiredCapacity` / `asgMaxCapacity` | `1/1/2` | Capacity targets for the Auto Scaling group (validated to keep `min <= desired <= max`). |
| `workspacePath` | `/workspaces/nightshift` | Path to mount the shared EFS workspace on the instances. |
| `workspaceUid` / `workspaceGid` | `1000` | POSIX owner used when provisioning the EFS access point. |
| `efsThroughputMode` | `elastic` | Accepts `elastic`, `bursting`, or `provisioned` (requires `efsProvisionedThroughputMibps`). |
| `logsBucketRetentionDays` | `90` | Lifecycle rule for expiring S3 objects (set to `0` to disable). |

Add `efsProvisionedThroughputMibps` if you choose the `provisioned` throughput
mode. Any new parameter defaults should be documented here so operators can
understand the available knobs.
