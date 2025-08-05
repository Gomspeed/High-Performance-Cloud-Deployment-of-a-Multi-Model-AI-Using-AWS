import aws_cdk as core
import aws_cdk.assertions as assertions

from langflow_ui.langflow_ui_stack import LangflowUiStack

# example tests. To run these tests, uncomment this file along with the example
# resource in langflow_ui/langflow_ui_stack.py
def test_sqs_queue_created():
    app = core.App()
    stack = LangflowUiStack(app, "langflow-ui")
    template = assertions.Template.from_stack(stack)

#     template.has_resource_properties("AWS::SQS::Queue", {
#         "VisibilityTimeout": 300
#     })
