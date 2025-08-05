#!/usr/bin/env python3
import os

import aws_cdk as cdk
from langflow_ui.langflow_ui_stack import LangflowUiStack

app = cdk.App()

synth = cdk.DefaultStackSynthesizer(
    qualifier="kyn",
    file_assets_bucket_name="kyn-bootstrap-bucket",
    bucket_prefix=""
)

LangflowUiStack(app, "LangflowUiStack",
    synthesizer=synth,
    # env=cdk.Environment(account=os.getenv("CDK_DEFAULT_ACCOUNT"),
    #                     region=os.getenv("CDK_DEFAULT_REGION"))
)

app.synth()
