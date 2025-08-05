#!/usr/bin/env python3
from aws_cdk import (
    Stack,
    Duration,
    CfnOutput,
    aws_ec2            as ec2,
    aws_ecs            as ecs,
    aws_ecs_patterns   as ecs_patterns,
    aws_wafv2          as wafv2,
    aws_cloudwatch     as cloudwatch,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

class LangflowUiStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # 1) VPC (public + private subnets)
        vpc = ec2.Vpc(self, "Vpc",
            max_azs=2,
            nat_gateways=1,
            restrict_default_security_group=False
        )

        # 2) ECS Cluster + EC2 Auto Scaling Group
        cluster = ecs.Cluster(self, "EcsCluster", vpc=vpc)
        cluster.add_capacity("Ec2Asg",
            instance_type=ec2.InstanceType("t3.small"),
            desired_capacity=2,
            min_capacity=1,
            max_capacity=4
        )

        # 3) (optional) OpenAI secret left in case you add back later
        openai_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "OpenAIKeySecret", "langflow/openai-api-key"
        )

        # 4) ALB‐backed EC2 Service using the ECS sample image
        service = ecs_patterns.ApplicationLoadBalancedEc2Service(
            self, "SampleService",
            cluster=cluster,
            desired_count=2,
            memory_limit_mib=512,
            public_load_balancer=True,
            listener_port=80,
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ecs.ContainerImage.from_registry("amazon/amazon-ecs-sample"),
                container_port=80,
                # no special env needed
                environment={},
                # no secrets in sample
                secrets={},
            ),
            health_check_grace_period=Duration.seconds(60),
        )

        # 4a) Health‐check: default path "/" is fine for the sample
        service.target_group.configure_health_check(
            path="/",
            healthy_http_codes="200",
            interval=Duration.seconds(30),
            timeout=Duration.seconds(5),
            healthy_threshold_count=2,
            unhealthy_threshold_count=5,
        )

        # 5) Auto‐scale on CPU (50% target)
        scalable = service.service.auto_scale_task_count(
            min_capacity=2,
            max_capacity=10
        )
        scalable.scale_on_cpu_utilization("CpuScaling",
            target_utilization_percent=50,
            scale_in_cooldown=Duration.seconds(60),
            scale_out_cooldown=Duration.seconds(60)
        )

        # 6) WAFv2 Web ACL with managed rule sets
        web_acl = wafv2.CfnWebACL(self, "WebAcl",
            scope="REGIONAL",
            default_action=wafv2.CfnWebACL.DefaultActionProperty(allow={}),
            visibility_config=wafv2.CfnWebACL.VisibilityConfigProperty(
                cloud_watch_metrics_enabled=True,
                sampled_requests_enabled=True,
                metric_name="WebAcl"
            ),
            rules=[
                wafv2.CfnWebACL.RuleProperty(
                    name="CommonRuleSet", priority=0,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
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
                    name="SQLiRuleSet", priority=1,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
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
                    name="BadInputsRuleSet", priority=2,
                    override_action=wafv2.CfnWebACL.OverrideActionProperty(none={}),
                    statement=wafv2.CfnWebACL.StatementProperty(
                        managed_rule_group_statement=wafv2.CfnWebACL.ManagedRuleGroupStatementProperty(
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
        wafv2.CfnWebACLAssociation(self, "WebAclAssoc",
            resource_arn=service.load_balancer.load_balancer_arn,
            web_acl_arn=web_acl.attr_arn
        )

        # 7) CloudWatch Dashboard
        dashboard = cloudwatch.Dashboard(self, "Dashboard",
            dashboard_name="EcsSampleDashboard"
        )
        cpu = service.service.metric_cpu_utilization(
            statistic="Average", period=Duration.minutes(1)
        )
        req = service.load_balancer.metric_request_count(
            statistic="Sum", period=Duration.minutes(1)
        )
        healthy = service.target_group.metric_healthy_host_count(
            statistic="Average", period=Duration.minutes(1)
        )
        blocked = cloudwatch.Metric(
            namespace="AWS/WAFV2",
            metric_name="BlockedRequests",
            dimensions_map={"ResourceArn": service.load_balancer.load_balancer_arn},
            statistic="Sum",
            period=Duration.minutes(5)
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(title="ECS CPU Utilization", left=[cpu]),
            cloudwatch.GraphWidget(title="ALB Request Count",    left=[req]),
            cloudwatch.GraphWidget(title="ALB Healthy Hosts",   left=[healthy]),
            cloudwatch.GraphWidget(title="WAF Blocked Requests",left=[blocked]),
        )

        # 8) Outputs
        CfnOutput(self, "LoadBalancerDNS",
            value=service.load_balancer.load_balancer_dns_name,
            description="Sample App DNS"
        )
        CfnOutput(self, "ClusterName",
            value=cluster.cluster_name,
            description="ECS Cluster Name"
        )
