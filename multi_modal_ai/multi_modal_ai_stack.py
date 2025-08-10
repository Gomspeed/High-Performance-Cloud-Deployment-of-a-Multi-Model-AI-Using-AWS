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
    aws_cloudwatch_actions as cw_actions,
    aws_certificatemanager as acm,
    aws_elasticloadbalancingv2 as elbv2,
    aws_route53 as route53,
    aws_route53_targets as targets,
    aws_autoscaling as autoscaling,
    aws_applicationautoscaling as appscaling,
    aws_sns as sns,
    aws_sns_subscriptions as subs,
    aws_iam as iam,
)
from constructs import Construct


NOTIFY_EMAIL = "gomezoluwatobi@gmail.com"  # you can remove or change later


class MultiModalAiStack(Stack):
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
            desired_capacity=2, min_capacity=1, max_capacity=4,
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

        # 3) (Optional) S3 bucket your app can read (keeping from previous stack)
        bucket = s3.Bucket(self, "KnowledgeBucket", removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True)

        # 4) Secrets (OpenAI + Google Gemini)
        openai_secret = secretsmanager.Secret.from_secret_name_v2(self, "OpenAIKeySecret", "multimodalai/openai-api-key")
        google_secret = secretsmanager.Secret.from_secret_name_v2(self, "GoogleKeySecret", "multimodalai/google-api-key")

        # 5) Lobe Chat image
        ui_image = ecs.ContainerImage.from_registry("lobehub/lobe-chat:latest")

        # 6) Hosted zone + 7) ACM cert
        hosted_zone = route53.HostedZone.from_lookup(self, "HostedZone", domain_name="naatsgroup.com")
        certificate = acm.Certificate(
            self, "AlbCert",
            domain_name="ridwan.naatsgroup.com",
            validation=acm.CertificateValidation.from_dns(hosted_zone),
        )

        # 8) ECS Service behind ALB (HTTPS + redirect), logs + exec
        service = ecs_patterns.ApplicationLoadBalancedEc2Service(
            self, "ChatUiService",
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
                container_port=3210,  # Lobe Chat default
                environment={
                    "NEXT_PUBLIC_ENABLE_AUTH": "false",   # quick start (turn on auth later if you want)
                    # You can also set defaults:
                    # "NEXT_PUBLIC_DEFAULT_PROVIDER": "openai",
                    # "NEXT_PUBLIC_DEFAULT_MODEL": "gpt-4o-mini",
                },
                secrets={
                    "OPENAI_API_KEY": ecs.Secret.from_secrets_manager(openai_secret, field="OPENAI_API_KEY"),
                    "GOOGLE_API_KEY": ecs.Secret.from_secrets_manager(google_secret, field="GOOGLE_API_KEY"),
                },
                log_driver=ecs.LogDrivers.aws_logs(stream_prefix="lobe-chat"),
            ),
            health_check_grace_period=Duration.seconds(120),
        )

        # 🔒 Security groups for EC2/bridge mode (dynamic host ports)
        alb_sg = service.load_balancer.connections.security_groups[0]
        asg_sg = asg.connections.security_groups[0]
        asg_sg.add_ingress_rule(
            peer=alb_sg,
            connection=ec2.Port.tcp_range(32768, 65535),
            description="Allow ALB to reach ECS tasks on dynamic host ports (bridge mode)",
        )
        alb_sg.add_egress_rule(
            peer=asg_sg,
            connection=ec2.Port.tcp_range(32768, 65535),
            description="Allow ALB egress to ECS instances on dynamic host ports",
        )

        # Health checks
        service.target_group.configure_health_check(
            path="/",
            port="traffic-port",
            healthy_http_codes="200-399",
            interval=Duration.seconds(30),
            timeout=Duration.seconds(20),
            healthy_threshold_count=2,
            unhealthy_threshold_count=5,
        )
        service.target_group.set_attribute("deregistration_delay.timeout_seconds", "30")
        service.load_balancer.set_attribute("idle_timeout.timeout_seconds", "120")

        # Task can read from the bucket (if you use it)
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

        # ===== Observability Pack (no canary) =====

        # A) ALB access logs → S3 (30-day retention) with proper policy for us-east-1
        alb_logs_bucket = s3.Bucket(
            self, "AlbAccessLogs",
            object_ownership=s3.ObjectOwnership.OBJECT_WRITER,   # required for ALB log delivery ACL
            lifecycle_rules=[s3.LifecycleRule(enabled=True, expiration=Duration.days(30))],
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            enforce_ssl=True,
        )
        elb_log_account = "127311923021"  # us-east-1 log delivery account
        alb_logs_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AWSLogDeliveryWrite",
                effect=iam.Effect.ALLOW,
                principals=[iam.ArnPrincipal(f"arn:aws:iam::{elb_log_account}:root")],
                actions=["s3:PutObject"],
                resources=[f"{alb_logs_bucket.bucket_arn}/alb-logs/AWSLogs/{self.account}/*"],
                conditions={"StringEquals": {"s3:x-amz-acl": "bucket-owner-full-control"}},
            )
        )
        alb_logs_bucket.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AWSLogDeliveryCheck",
                effect=iam.Effect.ALLOW,
                principals=[iam.ArnPrincipal(f"arn:aws:iam::{elb_log_account}:root")],
                actions=["s3:GetBucketAcl"],
                resources=[alb_logs_bucket.bucket_arn],
            )
        )
        service.load_balancer.set_attribute("access_logs.s3.enabled", "true")
        service.load_balancer.set_attribute("access_logs.s3.bucket", alb_logs_bucket.bucket_name)
        service.load_balancer.set_attribute("access_logs.s3.prefix", "alb-logs")
        if alb_logs_bucket.policy:
            service.load_balancer.node.add_dependency(alb_logs_bucket.policy)

        # B) SNS topic for alarms (+ permanent email subscription)
        alerts_topic = sns.Topic(self, "AlertsTopic")
        if NOTIFY_EMAIL:
            alerts_topic.add_subscription(subs.EmailSubscription(NOTIFY_EMAIL))

        # C) Metrics for alarms & dashboard
        tg = service.target_group
        lb = service.load_balancer

        p95_latency = tg.metric_target_response_time(statistic="p95", period=Duration.minutes(1))
        unhealthy_hosts = tg.metric_unhealthy_host_count(statistic="Average", period=Duration.minutes(1))
        http_target_5xx = cloudwatch.Metric(
            namespace="AWS/ApplicationELB", metric_name="HTTPCode_Target_5XX_Count",
            dimensions_map={"TargetGroup": tg.target_group_full_name, "LoadBalancer": lb.load_balancer_full_name},
            statistic="Sum", period=Duration.minutes(1)
        )
        http_elb_5xx = cloudwatch.Metric(
            namespace="AWS/ApplicationELB", metric_name="HTTPCode_ELB_5XX_Count",
            dimensions_map={"LoadBalancer": lb.load_balancer_full_name},
            statistic="Sum", period=Duration.minutes(1)
        )

        # D) Alarms → SNS
        cloudwatch.Alarm(
            self, "HighP95Latency",
            metric=p95_latency, threshold=1.0,
            evaluation_periods=3, datapoints_to_alarm=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            alarm_description="p95 target response time > 1s",
        ).add_alarm_action(cw_actions.SnsAction(alerts_topic))

        cloudwatch.Alarm(
            self, "UnhealthyHostsAlarm",
            metric=unhealthy_hosts, threshold=0.5,
            evaluation_periods=2, datapoints_to_alarm=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            alarm_description="Any target becomes unhealthy",
        ).add_alarm_action(cw_actions.SnsAction(alerts_topic))

        cloudwatch.Alarm(
            self, "Target5XXAlarm",
            metric=http_target_5xx, threshold=5,
            evaluation_periods=3, datapoints_to_alarm=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            alarm_description="Target group returning 5xx errors",
        ).add_alarm_action(cw_actions.SnsAction(alerts_topic))

        cloudwatch.Alarm(
            self, "Elb5XXAlarm",
            metric=http_elb_5xx, threshold=5,
            evaluation_periods=3, datapoints_to_alarm=2,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            alarm_description="ALB (frontend) returning 5xx errors",
        ).add_alarm_action(cw_actions.SnsAction(alerts_topic))

        # E) Dashboard
        dashboard = cloudwatch.Dashboard(self, "Dashboard", dashboard_name="ChainlitEcsDashboard")
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="ECS CPU Utilization (%)",
                left=[service.service.metric_cpu_utilization(statistic="Average", period=Duration.minutes(1))]
            ),
            cloudwatch.GraphWidget(
                title="ALB Request Count (Sum/min)",
                left=[lb.metric_request_count(statistic="Sum", period=Duration.minutes(1))]
            ),
            cloudwatch.GraphWidget(
                title="ALB Healthy (L) vs Unhealthy (R)",
                left=[tg.metric_healthy_host_count(statistic="Average", period=Duration.minutes(1))],
                right=[unhealthy_hosts],
            ),
            cloudwatch.GraphWidget(
                title="Target Response Time (p50 & p95)",
                left=[
                    tg.metric_target_response_time(statistic="p50", period=Duration.minutes(1)),
                    p95_latency
                ]
            ),
            cloudwatch.GraphWidget(
                title="HTTP 5xx (Target vs ELB)",
                left=[http_target_5xx],
                right=[http_elb_5xx],
            ),
        )

        # ===== /Observability Pack =====

        # 12) DNS: *****.com → ALB
        route53.ARecord(
            self, "ridwanAliasRecord",
            zone=hosted_zone,
            record_name="ridwan",
            target=route53.RecordTarget.from_alias(targets.LoadBalancerTarget(service.load_balancer)),
        )

        # 13) Outputs
        CfnOutput(self, "LoadBalancerDNS", value=service.load_balancer.load_balancer_dns_name, description="ALB DNS")
        CfnOutput(self, "AlertsSnsTopicArn", value=alerts_topic.topic_arn, description="Alerts SNS Topic")
