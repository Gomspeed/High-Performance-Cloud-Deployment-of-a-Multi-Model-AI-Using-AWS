#!/usr/bin/env python3
import os
import aws_cdk as cdk
from multi_modal_ai.multi_modal_ai_stack import MultiModalAiStack

app = cdk.App()

synth = cdk.DefaultStackSynthesizer(
    qualifier="kyn",
    file_assets_bucket_name="kyn-bootstrap-bucket",
    bucket_prefix=""
)

MultiModalAiStack(
    app, "MultiModalAiStack",
    synthesizer=synth,
    env=cdk.Environment(
        account=os.getenv("CDK_DEFAULT_ACCOUNT"),
        region=os.getenv("CDK_DEFAULT_REGION"),
    ),
)

app.synth()
