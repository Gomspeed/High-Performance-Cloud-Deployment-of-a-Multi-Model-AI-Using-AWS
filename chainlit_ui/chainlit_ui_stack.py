#!/usr/bin/env python3
from aws_cdk import (
    Stack,
    Duration,
    RemovalPolicy,
    CfnOutput,
    aws_ec2            as ec2,
    aws_ecs            as ecs,
    aws_ecs_patterns   as ecs_patterns,
    aws_s3             as s3,
    aws_secretsmanager as secretsmanager,
    aws_wafv2          as wafv2,
    aws_cloudwatch     as cloudwatch,
)
from constructs import Construct

class ChainlitUiStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        # 1) VPC with public + private subnets
        vpc = ec2.Vpc(
            self, "Vpc",
            max_azs=2,
            nat_gateways=1,
            restrict_default_security_group=False
        )

        # 2) ECS Cluster + EC2 Auto Scaling Group
        cluster = ecs.Cluster(self, "EcsCluster", vpc=vpc)
        cluster.add_capacity(
            "Ec2Asg",
            instance_type=ec2.InstanceType("t3.small"),
            desired_capacity=2,
            min_capacity=1,
            max_capacity=4
        )

        # 3) S3 Bucket for knowledge-base files
        bucket = s3.Bucket(
            self, "KnowledgeBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True
        )

        # 4) Import the JSON-format secret from Secrets Manager
        openai_secret = secretsmanager.Secret.from_secret_name_v2(
            self, "OpenAIKeySecret",
            "chainlit/openai-api-key"
        )

        # 5) Use a public UI image (no private creds needed)
        ui_image = ecs.ContainerImage.from_registry(
            "public.ecr.aws/k8o4e3d3/chatbot-ui:latest"
        )

        service = ecs_patterns.ApplicationLoadBalancedEc2Service(
            self, "ChatbotUiService",
            cluster=cluster,
            desired_count=2,
            memory_limit_mib=1024,
            public_load_balancer=True,
            listener_port=80,
            task_image_options=ecs_patterns.ApplicationLoadBalancedTaskImageOptions(
                image=ui_image,
                container_port=3000,
                environment={
                    "AWS_REGION":       self.region,
                    "KNOWLEDGE_BUCKET": bucket.bucket_name,
                },
                secrets={
                    # Extract only the OPENAI_API_KEY field from your JSON secret
                    "OPENAI_API_KEY": ecs.Secret.from_secrets_manager(
                        openai_secret,
                        field="OPENAI_API_KEY"
                    )
                },
            ),
            health_check_grace_period=Duration.seconds(60),
        )

        # Grant the ECS task role read-only access to the bucket
        bucket.grant_read(service.task_definition.task_role)

        # 6) WAFv2 Web ACL integration
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

        wafv2.CfnWebACLAssociation(
            self, "WebAclAssoc",
            resource_arn=service.load_balancer.load_balancer_arn,
            web_acl_arn=web_acl.attr_arn
        )

        # 7) CloudWatch Dashboard for key metrics
        dashboard = cloudwatch.Dashboard(
            self, "Dashboard",
            dashboard_name="ChainlitEcsDashboard"
        )

        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="ECS CPU Utilization (%)",
                left=[ service.service.metric_cpu_utilization(
                    statistic="Average", period=Duration.minutes(1)
                ) ]
            ),
            cloudwatch.GraphWidget(
                title="ALB Request Count",
                left=[ service.load_balancer.metric_request_count(
                    statistic="Sum", period=Duration.minutes(1)
                ) ]
            ),
            cloudwatch.GraphWidget(
                title="ALB Healthy Hosts",
                left=[ service.target_group.metric_healthy_host_count(
                    statistic="Average", period=Duration.minutes(1)
                ) ]
            ),
        )

        # 8) Stack outputs
        CfnOutput(
            self, "LoadBalancerDNS",
            value=service.load_balancer.load_balancer_dns_name,
            description="Public URL for the Chatbot UI"
        )
        CfnOutput(
            self, "KnowledgeBucketName",
            value=bucket.bucket_name,
            description="S3 bucket name for knowledge base files"
        )
