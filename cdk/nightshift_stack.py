"""
Phase 1.1 infrastructure stack for Nightshift.

Creates the dedicated VPC, storage, and compute required to run Nightshift in
AWS. All values originate from the per-instance YAML config (loaded via
``config.load_instance_config``) so operators can tune capacity without
editing stack code.
"""
from __future__ import annotations

import ipaddress
from typing import Any, Iterable

from aws_cdk import (
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_autoscaling as autoscaling,
    aws_ec2 as ec2,
    aws_efs as efs,
    aws_s3 as s3,
)
from constructs import Construct

from config import InstanceConfig, StackParameters


class NightshiftStack(Stack):
    """Nightshift infrastructure stack."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        config: InstanceConfig,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        settings = StackParameters.from_config(config)

        vpc = ec2.Vpc(
            self,
            "NightshiftVpc",
            ip_addresses=ec2.IpAddresses.cidr(settings.vpc_cidr),
            availability_zones=list(settings.availability_zones),
            nat_gateways=settings.nat_gateways,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=settings.public_subnet_mask,
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=settings.private_subnet_mask,
                ),
            ],
        )

        compute_sg = ec2.SecurityGroup(
            self,
            "ComputeSecurityGroup",
            vpc=vpc,
            allow_all_outbound=True,
            description="Controls inbound access to the Nightshift compute nodes.",
        )
        for cidr in settings.ssh_ingress_cidrs:
            network = ipaddress.ip_network(cidr, strict=False)
            peer = ec2.Peer.ipv4(cidr) if network.version == 4 else ec2.Peer.ipv6(cidr)
            compute_sg.add_ingress_rule(peer, ec2.Port.tcp(22), f"SSH from {cidr}")
        for cidr in settings.router_ingress_cidrs:
            network = ipaddress.ip_network(cidr, strict=False)
            peer = ec2.Peer.ipv4(cidr) if network.version == 4 else ec2.Peer.ipv6(cidr)
            compute_sg.add_ingress_rule(peer, ec2.Port.tcp(80), f"HTTP from {cidr}")
            compute_sg.add_ingress_rule(peer, ec2.Port.tcp(443), f"HTTPS from {cidr}")

        filesystem_sg = ec2.SecurityGroup(
            self,
            "EfsSecurityGroup",
            vpc=vpc,
            allow_all_outbound=False,
            description="Restricts EFS access to the Nightshift compute security group.",
        )
        filesystem_sg.add_ingress_rule(
            compute_sg,
            ec2.Port.tcp(2049),
            "Allow compute instances to mount the shared workspace",
        )

        throughput_mode = {
            "elastic": efs.ThroughputMode.ELASTIC,
            "bursting": efs.ThroughputMode.BURSTING,
            "provisioned": efs.ThroughputMode.PROVISIONED,
        }[settings.efs_throughput_mode]

        workspace_fs = efs.FileSystem(
            self,
            "SharedWorkspace",
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_group=filesystem_sg,
            encrypted=True,
            removal_policy=RemovalPolicy.RETAIN,
            performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
            throughput_mode=throughput_mode,
            provisioned_throughput_per_second=settings.efs_provisioned_throughput_mibps,
        )
        workspace_ap = efs.AccessPoint(
            self,
            "WorkspaceAccessPoint",
            file_system=workspace_fs,
            path=settings.workspace_path,
            create_acl=efs.Acl(
                owner_gid=str(settings.workspace_gid),
                owner_uid=str(settings.workspace_uid),
                permissions="750",
            ),
            posix_user=efs.PosixUser(
                gid=str(settings.workspace_gid),
                uid=str(settings.workspace_uid),
            ),
        )

        logs_bucket = s3.Bucket(
            self,
            "PromptLogsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=True,
            removal_policy=RemovalPolicy.RETAIN,
            auto_delete_objects=False,
        )
        if settings.logs_bucket_retention_days > 0:
            logs_bucket.add_lifecycle_rule(
                id="ExpireOldArchives",
                expiration=Duration.days(settings.logs_bucket_retention_days),
            )

        instance_type = ec2.InstanceType(settings.compute_instance_type)
        machine_image = ec2.MachineImage.generic_linux({config.aws_region: settings.compute_ami_id})

        user_data = ec2.UserData.for_linux()
        user_data.add_commands(
            "set -xe",
            f'MOUNT_DIR="{settings.workspace_path}"',
            f'FILE_SYSTEM_ID="{workspace_fs.file_system_id}"',
            "AZ=$(curl -s http://169.254.169.254/latest/meta-data/placement/availability-zone)",
            'REGION="${AZ%?}"',
            'PKG_MGR="yum"',
            'if command -v apt-get >/dev/null 2>&1; then PKG_MGR="apt"; fi',
            'if [[ "$PKG_MGR" == "yum" ]]; then sudo yum install -y amazon-efs-utils nfs-utils; '
            'else sudo apt-get update && sudo apt-get install -y amazon-efs-utils nfs-common; fi',
            'sudo mkdir -p "$MOUNT_DIR"',
            'ENDPOINT="${FILE_SYSTEM_ID}.efs.${REGION}.amazonaws.com:/"',
            'if ! grep -q "$ENDPOINT" /etc/fstab; then '
            'echo "$ENDPOINT $MOUNT_DIR efs defaults,_netdev" | sudo tee -a /etc/fstab >/dev/null; fi',
            "sudo mount -a -t efs defaults || true",
        )

        asg = autoscaling.AutoScalingGroup(
            self,
            "NightshiftAsg",
            vpc=vpc,
            min_capacity=settings.asg_min_capacity,
            max_capacity=settings.asg_max_capacity,
            desired_capacity=settings.asg_desired_capacity,
            instance_type=instance_type,
            machine_image=machine_image,
            allow_all_outbound=True,
            associate_public_ip_address=False,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS),
            security_group=compute_sg,
            user_data=user_data,
            block_devices=[
                autoscaling.BlockDevice(
                    device_name="/dev/xvda",
                    volume=autoscaling.BlockDeviceVolume.ebs(
                        settings.root_volume_size_gib,
                        encrypted=True,
                        volume_type=autoscaling.EbsDeviceVolumeType.GP3,
                    ),
                )
            ],
        )
        logs_bucket.grant_read_write(asg.role)

        self._emit_outputs(
            config=config,
            vpc=vpc,
            compute_sg=compute_sg,
            filesystem_sg=filesystem_sg,
            workspace_fs=workspace_fs,
            workspace_ap=workspace_ap,
            logs_bucket=logs_bucket,
            asg=asg,
        )

    def _emit_outputs(
        self,
        *,
        config: InstanceConfig,
        vpc: ec2.Vpc,
        compute_sg: ec2.SecurityGroup,
        filesystem_sg: ec2.SecurityGroup,
        workspace_fs: efs.FileSystem,
        workspace_ap: efs.AccessPoint,
        logs_bucket: s3.Bucket,
        asg: autoscaling.AutoScalingGroup,
    ) -> None:
        """Define CloudFormation outputs for operators."""
        CfnOutput(
            self,
            "InstanceName",
            value=config.instance_name,
            description="Identifier for the Nightshift deployment.",
        )
        CfnOutput(
            self,
            "InstanceParameters",
            value=", ".join(f"{k}={v}" for k, v in sorted(config.parameters.items())) or "none",
            description="Echoes custom parameters so operators can verify context wiring.",
        )
        CfnOutput(self, "VpcId", value=vpc.vpc_id, description="Nightshift VPC identifier.")
        CfnOutput(
            self,
            "VpcCidr",
            value=vpc.vpc_cidr_block,
            description="Base CIDR assigned to the VPC.",
        )
        CfnOutput(
            self,
            "PublicSubnetIds",
            value=self._join_tokens(subnet.subnet_id for subnet in vpc.public_subnets),
            description="Comma-separated list of public subnet IDs.",
        )
        CfnOutput(
            self,
            "PublicSubnetCidrs",
            value=self._join_tokens(subnet.ipv4_cidr_block for subnet in vpc.public_subnets),
            description="Public subnet IPv4 CIDRs.",
        )
        CfnOutput(
            self,
            "PrivateSubnetIds",
            value=self._join_tokens(subnet.subnet_id for subnet in vpc.private_subnets),
            description="Comma-separated list of private subnet IDs.",
        )
        CfnOutput(
            self,
            "PrivateSubnetCidrs",
            value=self._join_tokens(subnet.ipv4_cidr_block for subnet in vpc.private_subnets),
            description="Private subnet IPv4 CIDRs.",
        )
        CfnOutput(
            self,
            "ComputeSecurityGroupId",
            value=compute_sg.security_group_id,
            description="Security group protecting the Nightshift instances.",
        )
        CfnOutput(
            self,
            "EfsSecurityGroupId",
            value=filesystem_sg.security_group_id,
            description="Security group attached to the shared EFS file system.",
        )
        CfnOutput(
            self,
            "WorkspaceFileSystemId",
            value=workspace_fs.file_system_id,
            description="Persistent filesystem for shared Nightshift workspaces.",
        )
        CfnOutput(
            self,
            "WorkspaceAccessPointArn",
            value=workspace_ap.access_point_arn,
            description="EFS access point pinned to the Nightshift POSIX UID/GID.",
        )
        CfnOutput(
            self,
            "PromptLogsBucketName",
            value=logs_bucket.bucket_name,
            description="Bucket storing prompt/export archives.",
        )
        CfnOutput(
            self,
            "ComputeAsgName",
            value=asg.auto_scaling_group_name,
            description="Auto Scaling group hosting the compute nodes.",
        )

    @staticmethod
    def _join_tokens(values: Iterable[str]) -> str:
        return ", ".join(str(value) for value in values)
