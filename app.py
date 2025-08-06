# =========================================
# File: app.py
# =========================================
#!/usr/bin/env python3
import os
import aws_cdk as cdk
from chainlit_ui.chainlit_ui_stack import ChainlitUiStack

app = cdk.App()

synth = cdk.DefaultStackSynthesizer(
    qualifier="kyn",
    file_assets_bucket_name="kyn-bootstrap-bucket",
    bucket_prefix=""
)

ChainlitUiStack(app, "ChainlitUiStack",
    synthesizer=synth,
    env=cdk.Environment(account=os.getenv("CDK_DEFAULT_ACCOUNT"),
                        region=os.getenv("CDK_DEFAULT_REGION"))
)

app.synth()
