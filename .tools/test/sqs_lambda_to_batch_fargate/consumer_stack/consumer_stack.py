# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import os

from aws_cdk import Aws, Duration, Size, Stack
from aws_cdk import aws_batch_alpha as batch_alpha
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as _lambda
from aws_cdk import aws_lambda_event_sources as _event_sources
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_sqs as sqs
from constructs import Construct

# Raises KeyError if environment variable doesn't exist.
language_name = os.environ["LANGUAGE_NAME"]
producer_account_id = os.environ["PRODUCER_ACCOUNT_ID"]


class ConsumerStack(Stack):
    def __init__(self, scope: Construct, id: str, **kwargs) -> None:
        super().__init__(scope, id, **kwargs)

        #####################################################
        ##                                                 ##
        ##                 FANOUT COMPONENTS               ##
        ##           (SQS, SNS, and Subscriptions)         ##
        ##                                                 ##
        #####################################################

        # Locate Amazon Simple Notification Service (Amazon SNS) topic in the producer account.
        fanout_topic_name = "aws-weathertop-central-sns-fanout-topic"
        fanout_topic_arn = (
            f"arn:aws:sns:us-east-1:{producer_account_id}:{fanout_topic_name}"
        )
        sns_topic = sns.Topic.from_topic_arn(
            self, fanout_topic_name, topic_arn=fanout_topic_arn
        )

        # Define the Amazon Simple Queue Service (Amazon SQS) queue in this account.
        sqs_queue = sqs.Queue(self, f"BatchJobQueue-{language_name}")

        # Create an AWS Identity and Access Management (IAM) role for the SNS topic to send messages to the SQS queue.
        sns_topic_role = iam.Role(
            self,
            f"SNSTopicRole-{language_name}",
            assumed_by=iam.ServicePrincipal("sns.amazonaws.com"),
            description="Allows the SNS topic to send messages to the SQS queue in this account",
            role_name=f"SNSTopicRole-{language_name}",
        )

        # Policy to allow existing SNS topic to publish to new SQS queue.
        sns_topic_policy = iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            actions=["sqs:SendMessage"],
            resources=[sqs_queue.queue_arn],
            conditions={"ArnEquals": {"aws:SourceArn": fanout_topic_arn}},
        )

        # Create an SNS subscription for the SQS queue.
        subs.SqsSubscription(sqs_queue, raw_message_delivery=True).bind(sns_topic)
        sns_topic.add_subscription(subs.SqsSubscription(sqs_queue))

        # Add the Amazon SNS and Amazon SQS policy to the IAM role.
        sns_topic_role.add_to_policy(sns_topic_policy)

        # Define policy that allows cross-account Amazon SNS and Amazon SQS access.
        statement = iam.PolicyStatement()
        statement.add_resources(sqs_queue.queue_arn)
        statement.add_actions("sqs:*")
        statement.add_arn_principal(f"arn:aws:iam::{producer_account_id}:root")
        statement.add_arn_principal(f"arn:aws:iam::{Aws.ACCOUNT_ID}:root")
        statement.add_condition("ArnLike", {"aws:SourceArn": fanout_topic_arn})
        sqs_queue.add_to_resource_policy(statement)

        ################################################
        ##                                            ##
        ##            S3 BUCKET FOR LOGS              ##
        ##                                            ##
        ################################################

        bucket = s3.Bucket(
            self,
            "LogBucket",
            versioned=False,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
        )

        ################################################
        ##                                            ##
        ##            BATCH FARGATE JOBS              ##
        ##                                            ##
        ################################################

        batch_execution_role = iam.Role(
            self,
            f"BatchExecutionRole-{language_name}",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            inline_policies={
                "BatchLoggingPolicy": iam.PolicyDocument(
                    statements=[
                        iam.PolicyStatement(
                            effect=iam.Effect.ALLOW,
                            actions=[
                                "logs:CreateLogGroup",
                                "logs:CreateLogStream",
                                "logs:PutLogEvents",
                                "logs:DescribeLogStreams",
                            ],
                            resources=["arn:aws:logs:*:*:*"],
                        )
                    ]
                )
            },
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "job-function/SystemAdministrator"
                ),
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                ),
            ],
        )

        fargate_environment = batch_alpha.FargateComputeEnvironment(
            self,
            f"FargateEnv-{language_name}",
            vpc=ec2.Vpc.from_lookup(self, "Vpc", is_default=True),
        )

        container_image = ecs.EcrImage.from_registry(
            f"public.ecr.aws/b4v4v1s0/{language_name}:latest"
        )

        job_definition = batch_alpha.EcsJobDefinition(
            self,
            f"JobDefinition-{language_name}",
            container=batch_alpha.EcsFargateContainerDefinition(
                self,
                f"ContainerDefinition-{language_name}",
                image=container_image,
                execution_role=batch_execution_role,
                assign_public_ip=True,
                memory=Size.gibibytes(2),
                cpu=1,
            ),
            timeout=Duration.minutes(500),
        )

        job_queue = batch_alpha.JobQueue(self, f"JobQueue-{language_name}", priority=1)

        job_queue.add_compute_environment(fargate_environment, 1)

        #################################################
        ##                                             ##
        ##           BATCH LAMBDA FUNCTION             ##
        ##     (Triggers Batch job from SQS queue)     ##
        ##                                             ##
        #################################################

        # Execution role for AWS Lambda function to use.
        execution_role = iam.Role(
            self,
            f"BatchLambdaExecutionRole-{language_name}",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Allows Lambda function to submit jobs to Batch",
            role_name=f"BatchLambdaExecutionRole-{language_name}",
        )

        execution_role.add_to_policy(
            statement=iam.PolicyStatement(actions=["batch:*"], resources=["*"])
        )

        # Attach AWSLambdaBasicExecutionRole to the Lambda function's role
        execution_role.add_managed_policy(
            policy=iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSLambdaBasicExecutionRole"
            )
        )

        # Define the Lambda function.
        function = _lambda.Function(
            self,
            f"SubmitBatchJob-{language_name}",
            runtime=_lambda.Runtime.PYTHON_3_8,
            handler="submit_job.handler",
            role=execution_role,
            code=_lambda.Code.from_asset("lambda"),
            environment={
                "LANGUAGE_NAME": language_name,
                "JOB_QUEUE": job_queue.job_queue_arn,
                "JOB_DEFINITION": job_definition.job_definition_arn,
                "JOB_NAME": f"job-{language_name}",
            },
        )

        # Add the SQS queue as an event source for the Lambda function.
        function.add_event_source(_event_sources.SqsEventSource(sqs_queue))

        # Grant permissions to allow the function to receive messages from the queue.
        sqs_queue.grant_consume_messages(function)
        function.add_to_role_policy(
            statement=iam.PolicyStatement(
                actions=["sqs:ReceiveMessage"], resources=[sqs_queue.queue_arn]
            )
        )
        function.add_to_role_policy(
            statement=iam.PolicyStatement(
                actions=[
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=["*"],
            )
        )

        #################################################
        ##                                             ##
        ##            LOG LAMBDA FUNCTION              ##
        ##    (Processes logs and puts them to S3)     ##
        ##                                             ##
        #################################################

        # Execution role for AWS Lambda function to use.
        execution_role = iam.Role(
            self,
            f"LogsLambdaExecutionRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com"),
            description="Allows Lambda function to get logs from CloudWatch",
            role_name=f"LogsLambdaExecutionRole",
        )

        # Attach AWSLambdaBasicExecutionRole to the Lambda function's role.
        execution_role.add_managed_policy(
            policy=iam.ManagedPolicy.from_aws_managed_policy_name(
                "service-role/AWSLambdaBasicExecutionRole"
            )
        )

        # Grants ability to get logs from CloudWatch.
        execution_role.add_to_policy(
            statement=iam.PolicyStatement(
                actions=["logs:GetLogEvents", "logs:DescribeLogStreams"],
                resources=[f"arn:aws:logs:us-east-1:{Aws.ACCOUNT_ID}:*"],
            )
        )

        # Grants ability to get and put to local logs bucket.
        execution_role.add_to_policy(
            statement=iam.PolicyStatement(
                actions=["s3:PutObject", "s3:GetObject"],
                resources=[f"{bucket.bucket_arn}/*"],
            )
        )

        # Grants ability to write to cross-account log bucket.
        execution_role.add_to_policy(
            statement=iam.PolicyStatement(
                actions=["s3:PutObject", "s3:PutObjectAcl"],
                resources=["arn:aws:s3:::aws-weathertop-central-log-bucket/*"],
            )
        )

        # Define the Lambda function.
        lambda_function = _lambda.Function(
            self,
            "BatchJobCompleteLambda",
            runtime=_lambda.Runtime.PYTHON_3_8,
            handler="export_logs.handler",
            role=execution_role,
            code=_lambda.Code.from_asset("lambda"),
            environment={
                "LANGUAGE_NAME": language_name,
                "BUCKET_NAME": bucket.bucket_name,
                "PRODUCER_BUCKET_NAME": "aws-weathertop-central-log-bucket",
            },
        )

        batch_rule = events.Rule(
            self,
            "BatchAllEventsRule",
            event_pattern=events.EventPattern(source=["aws.batch"]),
        )

        # Add the Lambda function as a target for the CloudWatch Event Rule.
        batch_rule.add_target(targets.LambdaFunction(lambda_function))
