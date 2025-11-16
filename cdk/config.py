"""
Instance configuration loader for the Nightshift CDK app.

Each instance is described via YAML under cdk/instances/<name>.yml so
operators can parameterize regions, accounts, networking defaults, and tags
without editing the stack code directly.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Mapping, MutableMapping, Optional, Sequence, Tuple

import aws_cdk as cdk
import yaml

CDK_ROOT = Path(__file__).resolve().parent
INSTANCES_DIR = CDK_ROOT / "instances"


@dataclass
class InstanceConfig:
    """Container for per-instance stack configuration."""

    instance_name: str
    aws_account: str
    aws_region: str
    tags: Dict[str, str] = field(default_factory=dict)
    parameters: Dict[str, str] = field(default_factory=dict)

    @property
    def instance_slug(self) -> str:
        return self.instance_name.replace("_", "-")

    @property
    def aws_env(self) -> cdk.Environment:
        return cdk.Environment(account=self.aws_account, region=self.aws_region)


@dataclass(frozen=True)
class StackParameters:
    """Typed stack inputs resolved from the instance parameters map."""

    vpc_cidr: str
    public_subnet_mask: int
    private_subnet_mask: int
    max_azs: int
    availability_zones: Tuple[str, ...]
    nat_gateways: int
    ssh_ingress_cidrs: Tuple[str, ...]
    router_ingress_cidrs: Tuple[str, ...]
    compute_instance_type: str
    compute_ami_id: str
    root_volume_size_gib: int
    asg_min_capacity: int
    asg_max_capacity: int
    asg_desired_capacity: int
    workspace_path: str
    workspace_uid: int
    workspace_gid: int
    efs_throughput_mode: str
    efs_provisioned_throughput_mibps: Optional[float]
    logs_bucket_retention_days: int

    @classmethod
    def from_config(cls, config: InstanceConfig) -> "StackParameters":
        return cls._from_parameters(config.parameters, region=config.aws_region)

    @classmethod
    def _from_parameters(cls, params: Mapping[str, str], region: str) -> "StackParameters":
        def _get_str(key: str, default: Optional[str] = None) -> Optional[str]:
            value = params.get(key)
            if value is None:
                return default
            text = str(value).strip()
            return text or default

        def _get_int(key: str, default: Optional[int] = None, *, minimum: Optional[int] = None) -> int:
            raw = params.get(key)
            if raw is None:
                if default is None:
                    raise ValueError(f"Missing required integer parameter '{key}'")
                value = default
            else:
                try:
                    value = int(str(raw))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Invalid integer for parameter '{key}': {raw}") from exc
            if minimum is not None and value < minimum:
                raise ValueError(f"Parameter '{key}' must be >= {minimum} (got {value})")
            return value

        def _get_float(key: str) -> Optional[float]:
            raw = params.get(key)
            if raw is None:
                return None
            try:
                return float(str(raw))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid float for parameter '{key}': {raw}") from exc

        def _require_cidrs(key: str) -> Tuple[str, ...]:
            raw = params.get(key)
            if raw is None:
                raise ValueError(f"Missing required parameter '{key}'")

            cidr_list: Sequence[str]
            if isinstance(raw, Sequence) and not isinstance(raw, str):
                cidr_list = [str(item).strip() for item in raw if str(item).strip()]
            else:
                cidr_list = [chunk.strip() for chunk in re.split(r"[,\s]+", str(raw)) if chunk.strip()]

            if not cidr_list:
                raise ValueError(f"Parameter '{key}' must include at least one CIDR block")

            normalized: list[str] = []
            for entry in cidr_list:
                try:
                    network = ipaddress.ip_network(entry, strict=False)
                except ValueError as exc:
                    raise ValueError(f"Parameter '{key}' includes invalid CIDR '{entry}'") from exc
                normalized.append(str(network))
            return tuple(normalized)

        vpc_cidr = _get_str("vpcCidr", "10.42.0.0/16")
        try:
            ipaddress.ip_network(vpc_cidr, strict=False)
        except ValueError as exc:
            raise ValueError(f"Parameter 'vpcCidr' must be a valid CIDR (got {vpc_cidr})") from exc

        max_azs = _get_int("maxAzs", 2, minimum=2)
        nat_gateways = _get_int("natGateways", 1, minimum=1)

        asg_min = _get_int("asgMinCapacity", 1, minimum=0)
        asg_desired = _get_int("asgDesiredCapacity", asg_min, minimum=0)
        asg_max = _get_int("asgMaxCapacity", max(asg_desired, 1), minimum=1)
        if not (asg_min <= asg_desired <= asg_max):
            raise ValueError(
                "AutoScaling capacity settings must satisfy "
                "asgMinCapacity <= asgDesiredCapacity <= asgMaxCapacity "
                f"(got {asg_min} <= {asg_desired} <= {asg_max})"
            )

        throughput_mode = (_get_str("efsThroughputMode", "elastic") or "elastic").lower()
        allowed_modes = {"elastic", "bursting", "provisioned"}
        if throughput_mode not in allowed_modes:
            raise ValueError(
                f"efsThroughputMode must be one of {sorted(allowed_modes)}, got '{throughput_mode}'"
            )
        provisioned_throughput = _get_float("efsProvisionedThroughputMibps")
        if throughput_mode == "provisioned":
            if provisioned_throughput is None or provisioned_throughput <= 0:
                raise ValueError(
                    "efsProvisionedThroughputMibps must be > 0 when efsThroughputMode=provisioned"
                )
        else:
            provisioned_throughput = None

        def _resolve_ami_id() -> str:
            ami_candidate = _get_str("computeAmiId") or _get_str("codexAmiId")
            if not ami_candidate:
                raise ValueError(
                    "Missing compute AMI id. Set 'computeAmiId' (or legacy 'codexAmiId') "
                    "under the instance parameters map."
                )
            return ami_candidate

        az_candidates = params.get("availabilityZones")
        if az_candidates:
            if isinstance(az_candidates, Sequence) and not isinstance(az_candidates, str):
                az_list = [str(item).strip() for item in az_candidates if str(item).strip()]
            else:
                az_list = [chunk.strip() for chunk in re.split(r"[,\s]+", str(az_candidates)) if chunk.strip()]
            if len(az_list) < 2:
                raise ValueError("availabilityZones must list at least two AZ identifiers.")
            availability_zones = tuple(az_list[:max_azs])
        else:
            suffixes = "abcdefghijklmnopqrstuvwxyz"
            if max_azs > len(suffixes):
                raise ValueError(f"maxAzs cannot exceed {len(suffixes)} without explicit availabilityZones.")
            availability_zones = tuple(f"{region}{suffixes[i]}" for i in range(max_azs))

        router_ingress_cidrs = (
            _require_cidrs("routerCidrIngress") if params.get("routerCidrIngress") else tuple()
        )

        return cls(
            vpc_cidr=vpc_cidr,
            public_subnet_mask=_get_int("publicSubnetMask", 24, minimum=17),
            private_subnet_mask=_get_int("privateSubnetMask", 20, minimum=17),
            max_azs=max_azs,
            availability_zones=availability_zones,
            nat_gateways=nat_gateways,
            ssh_ingress_cidrs=_require_cidrs("sshCidrIngress"),
            router_ingress_cidrs=router_ingress_cidrs,
            compute_instance_type=_get_str("computeInstanceType", "t4g.small") or "t4g.small",
            compute_ami_id=_resolve_ami_id(),
            root_volume_size_gib=_get_int("instanceDiskSizeGiB", 64, minimum=20),
            asg_min_capacity=asg_min,
            asg_max_capacity=asg_max,
            asg_desired_capacity=asg_desired,
            workspace_path=_get_str("workspacePath", "/workspaces/nightshift") or "/workspaces/nightshift",
            workspace_uid=_get_int("workspaceUid", 1000, minimum=0),
            workspace_gid=_get_int("workspaceGid", 1000, minimum=0),
            efs_throughput_mode=throughput_mode,
            efs_provisioned_throughput_mibps=provisioned_throughput,
            logs_bucket_retention_days=_get_int("logsBucketRetentionDays", 90, minimum=0),
        )

def _load_yaml(path: Path) -> Mapping[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, MutableMapping):
        raise ValueError(f"Invalid config format for {path}")
    return data


def load_instance_config(instance_name: str) -> InstanceConfig:
    """Load <instance>.yml and hydrate an InstanceConfig."""
    candidate = INSTANCES_DIR / f"{instance_name}.yml"
    if not candidate.exists():
        raise FileNotFoundError(
            f"Instance config {candidate} is missing. Create it from "
            "cdk/instances/example-dev.yml before running CDK commands."
        )
    data = _load_yaml(candidate)

    def _require(key: str) -> str:
        value = str(data.get(key, "")).strip()
        if not value:
            raise ValueError(f"Instance config {candidate} missing required field '{key}'")
        return value

    tags = data.get("tags") or {}
    if not isinstance(tags, MutableMapping):
        raise ValueError(f"Expected tags mapping in {candidate}")
    params = data.get("parameters") or {}
    if not isinstance(params, MutableMapping):
        raise ValueError(f"Expected parameters mapping in {candidate}")

    return InstanceConfig(
        instance_name=_require("name"),
        aws_account=_require("aws_account"),
        aws_region=_require("aws_region"),
        tags={str(k): str(v) for k, v in tags.items()},
        parameters={str(k): str(v) for k, v in params.items()},
    )
