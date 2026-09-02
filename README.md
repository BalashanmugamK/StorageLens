# Intelligent Cloud Storage Cost Optimizer

An AWS-based system that analyzes cloud object access patterns and recommends cost-effective storage classes while considering storage, request, retrieval, and transition costs.

## Problem

Organizations often keep large collections of documents in frequently accessed storage even when many objects become rarely accessed over time. This can result in unnecessary storage costs.

This project explores an access-aware approach to cloud storage optimization.

## Proposed Solution

The system will:

1. Store documents in Amazon S3.
2. Store document metadata and access history in Amazon DynamoDB.
3. Analyze historical access patterns.
4. Estimate future access behavior.
5. Compare the expected cost of available S3 storage classes.
6. Recommend a cost-effective storage class.
7. Present recommendations and estimated savings through a dashboard.
8. Support scheduled optimization and, eventually, controlled automatic transitions.

## Architecture

The planned architecture uses:

- Amazon S3
- Amazon DynamoDB
- AWS Lambda
- Amazon API Gateway
- Amazon EventBridge
- Amazon CloudWatch
- Optional Amazon SNS

Infrastructure will be managed as code using AWS SAM.

## Optimization Objective

The optimizer will consider:

**Expected Monthly Cost = Storage Cost + Request Cost + Expected Retrieval Cost + Transition Cost**

The project will compare:

- All objects in S3 Standard
- Fixed age-based lifecycle policies
- Access-aware optimization

## Technology Stack

- Python
- AWS Lambda
- Amazon S3
- Amazon DynamoDB
- API Gateway
- EventBridge
- CloudWatch
- AWS SAM
- HTML/CSS/JavaScript
- Git/GitHub

## Project Status

?? Initial setup

The repository and development environment are being established before AWS implementation begins.

## Repository Structure

backend/          Application and Lambda code
optimization/     Cost model and optimization logic
frontend/         Dashboard
experiments/      Research experiments and results
infrastructure/   AWS SAM infrastructure
scripts/          Utility and deployment scripts
tests/            Automated tests
docs/             Architecture and project documentation
