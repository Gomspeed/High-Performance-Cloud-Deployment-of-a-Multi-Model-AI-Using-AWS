
# Welcome to your CDK Python project!

High-Performance Cloud Deployment of a Multi-Model AI Using AWS
This project deploys a highly available, auto-scaling, multi-model AI platform on AWS using the AWS Cloud Development Kit (CDK) in Python. The solution hosts multiple AI models (e.g., LLM, NLP, CV) on Amazon ECS (EC2 launch type) behind an Application Load Balancer (ALB) with HTTPS support via AWS Certificate Manager (ACM) and security via AWS Web Application Firewall (WAF).

Architecture Overview
Key AWS Services:

Amazon ECS (EC2 launch type) – Runs containerized AI inference workloads with auto-scaling.

Application Load Balancer (ALB) – Routes incoming HTTPS traffic securely to ECS tasks.

AWS Certificate Manager (ACM) – Issues and manages TLS certificates for HTTPS.

AWS WAF – Protects against OWASP Top 10 web vulnerabilities.

Amazon S3 – Stores ALB access logs and static assets.

Amazon CloudWatch – Monitors application and infrastructure performance.

AWS Secrets Manager – Securely stores API keys and credentials.

Amazon VPC – Provides a multi-AZ, secure network with public/private subnets and a NAT Gateway.

Network Layout:

Public Subnets – Host the ALB and NAT Gateways.

Private Subnets – Run ECS tasks (AI model containers).

Multi-AZ Deployment – Ensures high availability.

Features
Multi-model AI serving – Deploys multiple AI models in a single ECS cluster.

Auto-scaling – Adjusts compute resources based on traffic demand.

Secure HTTPS access – ACM-managed TLS certificates.

Layer-7 protection – AWS WAF with managed rule sets (CommonRuleSet, SQLiRuleSet, KnownBadInputsRuleSet).

Centralized logging – ALB access logs in S3 and ECS logs in CloudWatch.

Infrastructure as Code – Fully reproducible AWS setup via AWS CDK (Python).

Project Structure
graphql
Copy
Edit
multi_modal_ai/         # CDK stack definition (infrastructure code)
tests/                  # Unit tests for the CDK stack
app.py                  # Entry point for CDK application
cdk.json                # CDK configuration file
requirements.txt        # Python dependencies
Prerequisites
Python 3.9+

AWS CLI configured with an IAM user/role that has CDK deployment permissions

Node.js & AWS CDK CLI installed

Setup & Deployment
1. Clone the Repository
bash
Copy
Edit
git clone <repo-url>
cd High-Performance-Cloud-Deployment-of-a-Multi-Model-AI-Using-AWS
2. Create a Virtual Environment
bash
Copy
Edit
python -m venv .venv
Activate it:

macOS/Linux:

bash
Copy
Edit
source .venv/bin/activate
Windows (PowerShell):

powershell
Copy
Edit
.venv\Scripts\activate
3. Install Dependencies
bash
Copy
Edit
pip install -r requirements.txt
4. Deploy the Stack
bash
Copy
Edit
cdk synth   # Generate CloudFormation template
cdk deploy  # Deploy to AWS
Useful Commands
Command	Description
cdk ls	List all stacks in the app
cdk synth	Generate CloudFormation template
cdk deploy	Deploy the stack
cdk diff	Compare deployed stack with local code
cdk destroy	Delete the deployed resources

Live Demo


