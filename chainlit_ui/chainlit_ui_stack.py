#!/usr/bin/env python3
from aws_cdk import (
    Stack, Duration, RemovalPolicy, CfnOutput,
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
    aws_autoscaling as autoscaling,
    aws_applicationautoscaling as appscaling,
)
from constructs import Construct


class ChainlitUiStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # 1) VPC
        vpc = ec2.Vpc(self, "Vpc", max_azs=2, nat_gateways=1, restrict_default_security_group=False)

        # 2) ECS cluster + EC2 capacity provider
        cluster = ecs.Cluster(self, "EcsCluster", vpc=vpc)

        asg = autoscaling.AutoScalingGroup(
            self, "Ec2Asg",
            vpc=vpc,
            instance_type=ec2.InstanceType("t3.small"),
            machine_image=ecs.EcsOptimizedImage.amazon_linux2(),
            desired_capacity=2,
            min_capacity=1,
            max_capacity=4,
        )

        cp = ecs.AsgCapacityProvider(
            self, "AsgCapacityProvider",
            auto_scaling_group=asg,
            enable_managed_scaling=True,
            target_capacity_percent=80,
            enable_managed_termination_protection=False,
        )
        cluster.add_asg_capacity_provider(cp)
        cluster.add_default_capacity_provider_strategy([
            ecs.CapacityProviderStrategy(capacity_provider=cp.capacity_provider_name, weight=1)
        ])

        # 3) S3 bucket (read-only to tasks)
        bucket = s3.Bucket(self, "KnowledgeBucket", removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True)

        # 4) Secret (OPENAI key)
        openai_secret = secretsmanager.Secret.from_secret_name_v2(self, "OpenAIKeySecret", "chainlit/openai-api-key")

        # 5) App image
        ui_image = ecs.ContainerImage.from_registry("public.ecr.aws/k8o4e3d3/chatbot-ui:latest")

        # 6) DNS zone + 7) ACM cert
        hosted_zone = route53.HostedZone.from_lookup(self, "HostedZone", domain_name="naatsgroup.com")
        certificate = acm.Certificate(
            self, "AlbCert",
            domain_name="ridwan.naatsgroup.com",
            validation=acm.CertificateValidation.from_dns(hosted_zone),
        )

        # 8) ECS Service behind ALB (HTTPS + redirect), logs + exec
        service = ecs_patterns.ApplicationLoadBalancedEc2Service(
            self, "ChatbotUiService",
            cluster=cluster,
            desired_count=2,
            memory_limit_mib=1024,
            public_load_balancer=True,
            protocol=elbv2.ApplicationProtocol.HTTPS,
            certificate=certificate,
            redirect_http=True,
            enable_execute_command=True,
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ui_image,
                container_port=3000,
                environment={
                    "AWS_REGION": self.region,
                    "KNOWLEDGE_BUCKET": bucket.bucket_name,
                    "PORT": "3000",       # app should bind to 3000
                    "HOST": "0.0.0.0",    # listen on all interfaces
                },
                secrets={
                    "OPENAI_API_KEY": ecs.Secret.from_secrets_manager(openai_secret, field="OPENAI_API_KEY")
                },
                log_driver=ecs.LogDrivers.aws_logs(stream_prefix="chatbotui"),
            ),
            health_check_grace_period=Duration.seconds(120),
        )

        # 🔒 Security groups for EC2/bridge mode (dynamic host ports)
        alb_sg = service.load_balancer.connections.security_groups[0]
        asg_sg = asg.connections.security_groups[0]

        # Ingress: ALB -> instances on ephemeral host ports
        asg_sg.add_ingress_rule(
            peer=alb_sg,
            connection=ec2.Port.tcp_range(32768, 65535),
            description="Allow ALB to reach ECS tasks on dynamic host ports (bridge mode)",
        )
        # Egress: ALB -> instances on the same ports (defensive; helpful on restrictive setups)
        alb_sg.add_egress_rule(
            peer=asg_sg,
            connection=ec2.Port.tcp_range(32768, 65535),
            description="Allow ALB egress to ECS instances on dynamic host ports",
        )

        # Health checks for the app
        service.target_group.configure_health_check(
            path="/",                         # change if your app has a /health endpoint
            port="traffic-port",
            healthy_http_codes="200-399",
            interval=Duration.seconds(30),
            timeout=Duration.seconds(20),
            healthy_threshold_count=2,
            unhealthy_threshold_count=5,
        )
        # Faster deregistration to shorten updates/rollbacks
        service.target_group.set_attribute("deregistration_delay.timeout_seconds", "30")
        # Reduce false 504s on slow first responses
        service.load_balancer.set_attribute("idle_timeout.timeout_seconds", "120")

        # S3 read permission for tasks
        bucket.grant_read(service.task_definition.task_role)

        # 9) Task autoscaling (1..6) — CPU + request-rate
        scaling = service.service.auto_scale_task_count(min_capacity=1, max_capacity=6)
        scaling.scale_on_cpu_utilization(
            "CpuScaling",
            target_utilization_percent=30,
            scale_in_cooldown=Duration.seconds(60),
            scale_out_cooldown=Duration.seconds(60),
        )
        req_metric = service.target_group.metric_request_count_per_target(period=Duration.minutes(1))
        scaling.scale_on_metric(
            "RequestScaling",
            metric=req_metric,
            scaling_steps=[
                appscaling.ScalingInterval(upper=50, change=-1),
                appscaling.ScalingInterval(lower=100, change=+1),
                appscaling.ScalingInterval(lower=200, change=+2),
            ],
            adjustment_type=appscaling.AdjustmentType.CHANGE_IN_CAPACITY,
            cooldown=Duration.seconds(60),
        )

        # 10) WAF (US-only + managed rules)
        web_acl = wafv2.CfnWebACL(
            self, "WebAcl",
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True, sampled_requests_enabled=True, metric_name="WebAcl"
            ),
            rules=[
                wafv2.CfnWebACL.RuleProperty(
                    name="BlockNonUS", priority=0, action=wafv2.CfnWebACL.RuleActionProperty(block={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        not_statement=wafv2.CfnWebACL.NotStatementProperty(
                            statement=wafv2.CfnWebACL.StatementProperty(
                                geo_match_statement=wafv2.CfnWebACL.GeoMatchStatementProperty(country_codes=["US"])
                            )
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True, sampled_requests_enabled=True, metric_name="BlockNonUS"
                    ),
                ),
                wafv2.CfnWebACL.RuleProperty(
                    name="CommonRuleSet", priority=1, override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS", name="AWSManagedRulesCommonRuleSet"
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True, sampled_requests_enabled=True, metric_name="CommonRules"
                    ),
                ),
                wafv2.CfnWebACL.RuleProperty(
                    name="SQLiRuleSet", priority=2, override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS", name="AWSManagedRulesSQLiRuleSet"
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True, sampled_requests_enabled=True, metric_name="SQLiRules"
                    ),
                ),
                wafv2.CfnWebACL.RuleProperty(
                    name="BadInputsRuleSet", priority=3, override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
                            vendor_name="AWS", name="AWSManagedRulesKnownBadInputsRuleSet"
                        )
                    ),
                    visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                        cloud_watch_metrics_enabled=True, sampled_requests_enabled=True, metric_name="BadInputs"
                    ),
                ),
            ],
        )
        wafv2.CfnWebACLAssociation(self, "WebAclAssoc",
                                   resource_arn=service.load_balancer.load_balancer_arn,
                                   web_acl_arn=web_acl.attr_arn)

        # 11) Dashboard
        dash = cloudwatch.Dashboard(self, "Dashboard", dashboard_name="ChainlitEcsDashboard")
        dash.add_widgets(
            cloudwatch.GraphWidget(title="ECS CPU Utilization (%)",
                                   left=[service.service.metric_cpu_utilization(statistic="Average",
                                                                                period=Duration.minutes(1))]),
            cloudwatch.GraphWidget(title="ALB Request Count",
                                   left=[service.load_balancer.metric_request_count(statistic="Sum",
                                                                                    period=Duration.minutes(1))]),
            cloudwatch.GraphWidget(title="ALB Healthy Hosts",
                                   left=[service.target_group.metric_healthy_host_count(statistic="Average",
                                                                                        period=Duration.minutes(1))]),
        )

        # 12) DNS: ridwan.naatsgroup.com → ALB
        route53.ARecord(
            self, "RidwanAliasRecord",
            zone=hosted_zone,
            record_name="ridwan",
            target=route53.RecordTarget.from_alias(
                targets.LoadBalancerTarget(service.load_balancer)
            ),
        )

        # 13) Outputs
        CfnOutput(self, "LoadBalancerDNS", value=service.load_balancer.load_balancer_dns_name, description="ALB DNS")
        CfnOutput(self, "KnowledgeBucketName", value=bucket.bucket_name, description="S3 bucket")
