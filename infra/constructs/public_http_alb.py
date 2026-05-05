from collections.abc import Sequence
from typing import cast

from aws_cdk import (
    aws_certificatemanager as acm,
    aws_ec2 as ec2,
    aws_route53 as route53,
    aws_elasticloadbalancingv2 as elbv2,
    aws_route53_targets as route53_targets,
)
from constructs import Construct


class PublicHttpAlb(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        fqdn: str,
        a_record: str,
        zone: route53.IHostedZone,
        vpc: ec2.IVpc,
        additional_fqdns: Sequence[str] = (),
        internet_facing: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.security_group = ec2.SecurityGroup(
            self, "SecurityGroup", vpc=vpc, allow_all_outbound=True
        )
        ingress_peer = (
            ec2.Peer.any_ipv4()
            if internet_facing
            else ec2.Peer.ipv4(vpc.vpc_cidr_block)
        )
        self.security_group.add_ingress_rule(ingress_peer, ec2.Port.tcp(443), "HTTPS")
        self.security_group.add_ingress_rule(
            ingress_peer, ec2.Port.tcp(80), "HTTP redirect"
        )

        self.alb = elbv2.ApplicationLoadBalancer(
            self,
            "Alb",
            vpc=vpc,
            internet_facing=internet_facing,
            security_group=self.security_group,
        )

        self.certificate = acm.Certificate(
            self,
            "Certificate",
            domain_name=fqdn,
            subject_alternative_names=(additional_fqdns or None),
            validation=acm.CertificateValidation.from_dns(zone),
        )

        self.https_listener = self.alb.add_listener(
            "HttpsListener",
            port=443,
            certificates=[self.certificate],
            open=True,
        )

        self.http_listener = self.alb.add_listener("HttpListener", port=80, open=True)
        self.http_listener.add_action(
            "RedirectToHttps",
            action=elbv2.ListenerAction.redirect(
                protocol="HTTPS", port="443", permanent=True
            ),
        )

        alias_target = route53.RecordTarget.from_alias(
            cast(
                route53.IAliasRecordTarget,
                route53_targets.LoadBalancerTarget(self.alb),
            )
        )

        self.a_record = route53.ARecord(
            self,
            "AliasRecord",
            zone=zone,
            record_name=a_record,
            target=alias_target,
        )

        self.additional_a_records: list[route53.ARecord] = []
        for additional_fqdn in additional_fqdns:
            record_name = additional_fqdn.removesuffix(f".{zone.zone_name}")
            self.additional_a_records.append(
                route53.ARecord(
                    self,
                    f"AliasRecord-{record_name}",
                    zone=zone,
                    record_name=record_name,
                    target=alias_target,
                )
            )
