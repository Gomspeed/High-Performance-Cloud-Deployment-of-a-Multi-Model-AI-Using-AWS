#!/usr/bin/env python3
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_ec2 as ec2,
    aws_ecs as ecs,
    aws_ecs_patterns as ecs_patterns,
    aws_s3 as s3,
    aws_secretsmanager as secretsmanager,
    aws_wafv2 as wafv2,
    aws_cloudwatch as cloudwatch,
    aws_certificatemanager as acm,
    aws_elasticloadbalancingv2 as elbv2,
    aws_route53 as route53,
    aws_route53_targets as targets,
)
from constructs import Construct

class ChainlitUiStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # 1) VPC
        vpc = ec2.Vpc(
            self, "Vpc",
            max_azs=2,
            nat_gateways=1,
            restrict_default_security_group=False
        )

        # 2) ECS Cluster + EC2 Auto Scaling
        cluster = ecs.Cluster(self, "EcsCluster", vpc=vpc)
        cluster.add_capacity(
            "Ec2Asg",
            instance_type=ec2.InstanceType("t3.small"),
            desired_capacity=2,
            min_capacity=1,
            max_capacity=4
        )

        # 3) S3 Bucket for knowledge-base
        bucket = s3.Bucket(
            self, "KnowledgeBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True
        )

        # 4) Import OpenAI secret
        openai_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "OpenAIKeySecret",
            "chainlit/openai-api-key"
        )

        # 5) UI Container Image
        ui_image = ecs.ContainerImage.from_registry(
            "public.ecr.aws/k8o4e3d3/chatbot-ui:latest"
        )

        # 6) Lookup Route 53 Hosted Zone
        hosted_zone = route53.HostedZone.from_lookup(
            self, "HostedZone",
            domain_name="naatsgroup.com"
        )

        # 7) DNS-validated ACM Certificate for ridwan.naatsgroup.com
        certificate = acm.Certificate(
            self, "AlbCert",
            domain_name="ridwan.naatsgroup.com",
            validation=acm.CertificateValidation.from_dns(hosted_zone)
        )

        # 8) ECS Service w/ HTTPS + HTTP→HTTPS redirect
        service = ecs_patterns.ApplicationLoadBalancedEc2Service(
            self, "ChatbotUiService",
            cluster=cluster,
            desired_count=2,
            memory_limit_mib=1024,
            public_load_balancer=True,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            certificate=certificate,
            redirect_http=True,
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ui_image,
                container_port=3000,
                environment={
                    "AWS_REGION": self.region,
                    "KNOWLEDGE_BUCKET": bucket.bucket_name,
                },
                secrets={
                    "OPENAI_API_KEY": ecs.Secret.from_secrets_manager(
                        openai_secret,
                        field="OPENAI_API_KEY"
                    )
                }
            ),
            health_check_grace_period=Duration.seconds(60),
        )

        # Grant S3 read to the ECS task
        bucket.grant_read(service.task_definition.task_role)

        # 9) WAFv2 Web ACL
        web_acl = wafv2.CfnWebACL(
            self, "WebAcl",
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                sampled_requests_enabled=True,
                metric_name="WebAcl"
            ),
            rules=[
                # Block non-US
                wafv2.CfnWebACL.RuleProperty(
                    name="BlockNonUS", priority=0,
                    action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        not_statement=wafv2.CfnWebACL.NotStatementProperty(
                            statement=wafv2.CfnWebACL.StatementProperty(
                                geo_match_statement=wafv2.CfnWebACL.GeoMatchStatementProperty(
                                    country_codes=["US"]
                                )
                            )
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        sampled_requests_enabled=True,
                        metric_name="BlockNonUS"
                    )
                ),
                # AWS Managed Rules
                wafv2.CfnWebACL.RuleProperty(
                    name="CommonRuleSet", priority=1,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=
                            wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                                vendor_name="AWS",
                                name="AWSManagedRulesCommonRuleSet"
                            )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        sampled_requests_enabled=True,
                        metric_name="CommonRules"
                    )
                ),
                wafv2.CfnWebACL.RuleProperty(
                    name="SQLiRuleSet", priority=2,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=
                            wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                                vendor_name="AWS",
                                name="AWSManagedRulesSQLiRuleSet"
                            )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        sampled_requests_enabled=True,
                        metric_name="SQLiRules"
                    )
                ),
                wafv2.CfnWebACL.RuleProperty(
                    name="BadInputsRuleSet", priority=3,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=
                            wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                                vendor_name="AWS",
                                name="AWSManagedRulesKnownBadInputsRuleSet"
                            )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True,
                        sampled_requests_enabled=True,
                        metric_name="BadInputs"
                    )
                ),
            ]
        )
        wafv2.CfnWebACLAssociation(
            self, "WebAclAssoc",
            resource_arn=service.load_balancer.load_balancer_arn,
            web_acl_arn=web_acl.attr_arn
        )

        # 10) CloudWatch Dashboard
        dashboard = cloudwatch.Dashboard(
            self, "Dashboard",
            dashboard_name="ChainlitEcsDashboard"
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="ECS CPU Utilization (%)",
                left=[service.service.metric_cpu_utilization(
                    statistic="Average", period=Duration.minutes(1)
                )]
            ),
            cloudwatch.GraphWidget(
                title="ALB Request Count",
                left=[service.load_balancer.metric_request_count(
                    statistic="Sum", period=Duration.minutes(1)
                )]
            ),
            cloudwatch.GraphWidget(
                title="ALB Healthy Hosts",
                left=[service.target_group.metric_healthy_host_count(
                    statistic="Average", period=Duration.minutes(1)
                )]
            ),
        )

        # 11) Route 53 Alias Record for ridwan.naatsgroup.com
        route53.ARecord(
            self, "RidwanAliasRecord",
            zone=hosted_zone,
            record_name="ridwan",
            target=route53.RecordTarget.from_alias(
                targets.LoadBalancerTarget(service.load_balancer)
            )
        )

        # 12) Stack Outputs
        CfnOutput(
            self, "LoadBalancerDNS",
            value=service.load_balancer.load_balancer_dns_name,
            description="ALB DNS name"
        )
        CfnOutput(
            self, "KnowledgeBucketName",
            value=bucket.bucket_name,
            description="S3 bucket for knowledge base"
        )
