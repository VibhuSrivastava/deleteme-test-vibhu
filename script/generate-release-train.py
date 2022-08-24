#!/usr/bin/env python3
"""
Testable locally with:
python3 script/generate-release-train.py \
  --chart deleteme-vibhu \
  --repo-name deleteme-test-vibhu \
  --repo-sha1 4b3dda6 \
  --prune false \
  --skip-deployment-to-production true \
  --release-train-disabled true
"""

from __future__ import print_function

import argparse
import base64
import datetime
import urllib.error
import urllib.parse
import urllib.request
from os import path

import sys
import time
import yaml  # pip install pyyaml

# from seb_deployment_policy import is_seb_restricted_date
import subprocess
import os

GLOBAL_PRODUCTION = "cluster6-production"
OXFORD_TESTING = "cluster9-testing"

# Deploy to these by adding the "deploy-to-non-aws"
ON_PREM_CLUSTERS = [
    "cluster1-testing",
    "cluster1-production",
]

# Deploy to the se with the "deploy-to-cluster10" config
AGGREGATION_CLUSTERS = [
    "cluster10-staging",
    "cluster10-production",
]

with open(
    path.join(path.dirname(path.realpath(__file__)), "clusters.yaml"), "r"
) as clusters:
    DEFAULT_STRATEGY_ALL_CLUSTERS = yaml.load(clusters, Loader=yaml.SafeLoader)


def filter_production_clusters(cluster_groups):
    new_cluster_groups = []
    for g in cluster_groups:
        new_cluster_groups.append(
            [c for c in g if c["environment"] != "production"]
        )
    return new_cluster_groups


def extract_production_clusters(cluster_groups):
    new_cluster_groups = []
    for g in cluster_groups:
        new_cluster_groups.append(
            [c for c in g if c["environment"] == "production"]
        )
    return new_cluster_groups


def main():
    parser = cli_args_parser()
    args = parser.parse_args()

    chart_yaml = False
    with open("script/Chart.yaml", "r") as fpChart:
        chart_yaml = fpChart.read()

    version = args.version if args.version else ""
    prune = "True" if args.prune.lower() == "true" else "False"
    no_external_tests = str(
        args.no_external_tests and args.no_external_tests.lower() == "true"
    )
    no_email_notifications = str(
        args.no_email_notifications and args.no_email_notifications.lower() == "true"
    )
    skip_deployment_to_production = True if args.skip_deployment_to_production.lower() == "true" else False
    release_train_disabled = True if args.release_train_disabled.lower() == "true" else False

    chart = yaml.load(chart_yaml, Loader=yaml.SafeLoader)
    deployment = chart["tink"]["deployment"]

    trigger_step_common_args = {
        "chart": args.chart,
        "repo_name": args.repo_name,
        "repo_sha1": args.repo_sha1,
        "prune": prune,
        "version": version,
        "no_external_tests": no_external_tests,
        "no_email_notifications": no_email_notifications,
        "pull_requests": args.pull_requests or "",
        "trigger_salt_deploy": "True"
        if deployment.get("trigger_salt_deploy", False)
        else "False",
        "chart_version": args.chart_version,
        "chart_repo": args.chart_repo,
        "chart_yaml": chart,
    }

    cluster_groups = []

    if deployment["strategy"] == "custom":
        cluster_groups = cluster_groups_custom(strategy=deployment["custom_strategy"])

    if deployment["strategy"] == "default":
        cluster_groups = cluster_groups_default(
            deploy_to_cluster10=deployment.get("to_cluster10", False),
            deploy_to_on_prem=deployment.get("to_on_prem", False),
            deploy_to_cluster6=deployment.get("to_cluster6", False),
            deploy_to_cluster9_testing=deployment.get("to_cluster9_testing", False),
            explicit_exclude_clusters=deployment.get("exclude_clusters", []),
            deploy_to_additional_clusters=deployment.get("additional_clusters", []),
        )

    chart_end_to_end_test_configuration = chart["tink"].get("end_to_end_tests", {})

    generate_hotfix_release_triggers(
        cluster_groups,
        trigger_step_common_args,
        chart_end_to_end_test_configuration,
    )

    if release_train_disabled:
        print_disabled_block_step("The release-train for this chart has been disabled via the "
                                  "chart-dashboard. Continue?")

    non_frozen_cluster_groups = cluster_groups

    if skip_deployment_to_production:
        non_production_cluster_groups = filter_production_clusters(non_frozen_cluster_groups)
        production_cluster_groups = extract_production_clusters(non_frozen_cluster_groups)

        generate_train_triggers(
            non_production_cluster_groups,
            trigger_step_common_args,
            chart_end_to_end_test_configuration,
        )
        print_disabled_block_step("The release-train for production environments has been disabled. Continue?")
        generate_train_triggers(
            production_cluster_groups,
            trigger_step_common_args,
            chart_end_to_end_test_configuration,
        )
    else:
        generate_train_triggers(
            non_frozen_cluster_groups,
            trigger_step_common_args,
            chart_end_to_end_test_configuration,
        )


def cli_args_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", type=str, required=False)
    parser.add_argument("--repo-name", type=str, required=True)
    parser.add_argument("--repo-sha1", type=str, required=False)
    parser.add_argument("--chart", type=str, required=True)
    parser.add_argument("--prune", type=str, required=True)
    parser.add_argument("--no-external-tests", type=str, required=False)
    parser.add_argument("--no-email-notifications", type=str, required=False)
    parser.add_argument("--skip-deployment-to-production", type=str, required=False)
    parser.add_argument("--release-train-disabled", type=str, required=False)

    parser.add_argument(
        "--chart-control-hostname",
        type=str,
        required=False,
        default="",
    )
    parser.add_argument("--pull-requests", type=str, required=False)
    parser.add_argument("--chart-repo", type=str, required=False)
    parser.add_argument("--chart-version", type=str, required=False)
    # Set it to the path to the root of a git repo, and the git repo clone/checkout part
    # of this script will be skipped
    #
    # Use this argument for development purposes:
    parser.add_argument("--repo-path-override", type=str, required=False)
    return parser


def print_disabled_block_step(message):
    print(
        yaml.dump(
            [
                {
                    "block": message
                }
            ],
            default_flow_style=False,
            default_style='"',
        )
    )


def cluster_groups_custom(strategy):
    res_groups = []
    for step in strategy:
        res_groups.append(step["group"])
    return res_groups


def end_to_end_test_enabled(clusters_envs, cluster, env):
    for cluster_env in clusters_envs.get("environments", []):
        if cluster == cluster_env.get("cluster", "") and env == cluster_env.get(
            "environment", ""
        ):
            return True

    return False


def cluster_groups_default(
    deploy_to_cluster10,
    deploy_to_on_prem,
    deploy_to_cluster6,
    deploy_to_cluster9_testing,
    explicit_exclude_clusters=[],
    deploy_to_additional_clusters=[],
):
    res_groups = []

    for group in DEFAULT_STRATEGY_ALL_CLUSTERS:

        res_group = []

        for env in group:
            combined_name = env["cluster"] + "-" + env["environment"]

            # Skip if this cluster is on the exclude list
            if combined_name in explicit_exclude_clusters:
                continue

            # Skip on prem
            if not deploy_to_on_prem and combined_name in ON_PREM_CLUSTERS:
                continue

            # Skip cluster10
            if not deploy_to_cluster10 and combined_name in AGGREGATION_CLUSTERS:
                continue

            # Skip cluster6
            if not deploy_to_cluster6 and combined_name == GLOBAL_PRODUCTION:
                continue

            # Skip cluster9-testing
            if not deploy_to_cluster9_testing and combined_name == OXFORD_TESTING:
                continue

            res_group.append(env)

        if len(res_group) > 0:
            res_groups.append(res_group)

    if deploy_to_additional_clusters and len(deploy_to_additional_clusters) > 0:
        res_groups.append(deploy_to_additional_clusters)

    return res_groups


def generate_hotfix_release_triggers(
    cluster_groups,
    trigger_step_common_args,
    chart_end_to_end_test_configuration,
):
    all_steps = []

    for group in cluster_groups:
        for env in group:
            end_to_end_tests = (
                ",".join(chart_end_to_end_test_configuration.get("tests", []))
                if end_to_end_test_enabled(
                    chart_end_to_end_test_configuration,
                    env["cluster"],
                    env["environment"],
                )
                else ""
            )

            step = trigger_step(
                name_prefix="Blocked hotfix release to",
                block="True",
                _async=True,
                cluster=env["cluster"],
                environment=env["environment"],
                end_to_end_tests=end_to_end_tests,
                **trigger_step_common_args,
            )
            all_steps.append(step)

    if all_steps:
        print(yaml.dump(all_steps, default_flow_style=False))


def generate_train_triggers(
    cluster_groups,
    trigger_step_common_args,
    chart_end_to_end_test_configuration,
):
    all_yaml = []

    for group in cluster_groups:
        for env in group:
            end_to_end_tests = (
                ",".join(chart_end_to_end_test_configuration.get("tests", []))
                if end_to_end_test_enabled(
                    chart_end_to_end_test_configuration,
                    env["cluster"],
                    env["environment"],
                )
                else ""
            )

            step_yaml = trigger_step(
                name_prefix="Deploy to",
                block="False",
                _async=False,
                cluster=env["cluster"],
                environment=env["environment"],
                end_to_end_tests=end_to_end_tests,
                **trigger_step_common_args,
            )

            all_yaml.append(step_yaml)

        all_yaml.append("wait")

    print(yaml.dump(all_yaml, default_flow_style=False))


def trigger_step(
    name_prefix,
    block,
    _async,
    cluster,
    environment,
    end_to_end_tests,
    repo_sha1,
    repo_name,
    prune,
    version,
    chart,
    no_external_tests,
    no_email_notifications,
    pull_requests,
    trigger_salt_deploy,
    chart_repo,
    chart_version,
    chart_yaml,
):
    obj = {
        "name": f"{name_prefix} {cluster}-{environment}",
        "trigger": "apply-kubernetes-charts",
        "async": _async,
        "build": {
            "message": f"Deploy {chart} to {cluster}-{environment}",
            "commit": "HEAD",
            "branch": f"{cluster}-{environment}-{chart}",
            "env": {
                "CHART": chart,
                "CLUSTER": cluster,
                "ENVIRONMENT": environment,
                "REPO_NAME": repo_name,
                "REPO_SHA1": repo_sha1,
                "DRY_RUN": "False",
                "PRUNE": prune,
                "BLOCK": block,
                "VERSION": version,
                "NO_EXTERNAL_TESTS": no_external_tests,
                "NO_EMAIL_NOTIFICATIONS": no_email_notifications,
                "TRIGGER_SALT_DEPLOY": trigger_salt_deploy,
                "END_TO_END_TESTS": end_to_end_tests,
                "CHART_REPO": chart_repo,
                "CHART_VERSION": chart_version,
            },
            "meta_data": {
                "chart": chart,
                "environment": environment,
                "cluster": cluster,
                "repository-owner": "tink-ab",
                "repository-name": repo_name,
                "pull-request-ids": pull_requests,
                "chart-yaml": yaml.dump(chart_yaml),
            },
        },
    }

    return obj


def run_command(command):
    subprocess.run(command)


if __name__ == "__main__":
    sys.exit(main())

