#!/usr/bin/env python3
"""
Testable locally with:
python3 .charts/buildkite-agent/agent-scripts/generate-release-train.py \
  --chart prometheus \
  --repo-name tink-infrastructure \
  --repo-sha1 abc12 \
  --version 100 \
  --repo-path-override ~/src/tink-infrastructure \
  --prune false
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

GLOBAL_PRODUCTION = "global-production"
OXFORD_TESTING = "oxford-testing"

BLACKLISTED_CLUSTERS = [
    "kerry",
    "kirkby",
]

# Deploy to these by adding the "deploy-to-non-aws"
ON_PREM_CLUSTERS = [
    "cornwall-testing",
    "cornwall-production",
]

# Deploy to the se with the "deploy-to-aggregation" config
AGGREGATION_CLUSTERS = [
    "aggregation-staging",
    "aggregation-production",
]

with open(
    path.join(path.dirname(path.realpath(__file__)), "clusters.yaml"), "r"
) as clusters:
    DEFAULT_STRATEGY_ALL_CLUSTERS = yaml.load(clusters, Loader=yaml.SafeLoader)


def filter_deprecated_clusters(cluster_groups):
    new_cluster_groups = []
    for g in cluster_groups:
        new_cluster_groups.append(
            [c for c in g if c["cluster"] not in BLACKLISTED_CLUSTERS]
        )
    return new_cluster_groups


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

    # workflow_run_url = os.getenv("WORKFLOW_RUN_URL")
    # commit_url = f"https://github.com/tink-ab/{args.repo_name}/commit/{args.repo_sha1}"

    # if workflow_run_url is not None:
    #     create_annotations(
    #         "workflow_url",
    #         "info",
    #         f"Triggered by [Github Action]({workflow_run_url}) for [commit]({commit_url})",
    #     )
    # else:
    #     create_annotations(
    #         "commit_url",
    #         "info",
    #         f"Triggered for [commit]({commit_url})",
    #     )

    chart_yaml = False

    # if args.repo_path_override:
    # chart_config_path = path.join(args.repo_path_override, ".charts", args.chart, "Chart.yaml")
    with open("script/Chart.yaml", "r") as fpChart:
        chart_yaml = fpChart.read()
    # else:
        # chart_yaml = download_chart_yaml(args.repo_name, args.repo_sha1, args.chart)

    version = args.version if args.version else ""
    prune = "True" if args.prune.lower() == "true" else "False"
    no_external_tests = str(
        args.no_external_tests and args.no_external_tests.lower() == "true"
    )
    no_email_notifications = str(
        args.no_email_notifications and args.no_email_notifications.lower() == "true"
    )
    skip_deployment_to_production = str(
        args.skip_deployment_to_production and args.skip_deployment_to_production.lower() == "true"
    )

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
            deploy_to_aggregation=deployment.get("to_aggregation", False),
            deploy_to_on_prem=deployment.get("to_on_prem", False),
            deploy_to_global=deployment.get("to_global", False),
            deploy_to_oxford_testing=deployment.get("to_oxford_testing", False),
            explicit_exclude_clusters=deployment.get("exclude_clusters", []),
            deploy_to_additional_clusters=deployment.get("additional_clusters", []),
        )

    cluster_groups = filter_deprecated_clusters(cluster_groups)

    chart_end_to_end_test_configuration = chart["tink"].get("end_to_end_tests", {})

    generate_hotfix_release_triggers(
        cluster_groups,
        trigger_step_common_args,
        chart_end_to_end_test_configuration,
    )

    # Check if the release train is "disabled" via the chart-dashboard
    add_block_step_if_disabled(args.chart, args.chart_control_hostname, args.repo_name)

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
    parser.add_argument("--repo-sha1", type=str, required=True)
    parser.add_argument("--chart", type=str, required=True)
    parser.add_argument("--prune", type=str, required=True)
    parser.add_argument("--no-external-tests", type=str, required=False)
    parser.add_argument("--no-email-notifications", type=str, required=False)
    parser.add_argument("--skip-deployment-to-production", type=str, required=False)

    parser.add_argument(
        "--chart-control-hostname",
        type=str,
        required=False,
        default="tink-release-train-int.release-train.svc.cluster.local",
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


def add_block_step_if_disabled(chart, chart_control_hostname, repo_name):
    for x in range(0, 10):
        try:
            quoted_chart = urllib.parse.quote(chart)
            quoted_repo_name = urllib.parse.quote(repo_name)
            url = f"http://{chart_control_hostname}/train-enabled?chart={quoted_chart}&repo={quoted_repo_name}"
            print(url, file=sys.stderr)
            status_code = url_status(url)
        except urllib.error.HTTPError as e:
            # Chart name is used in another repo, this is a failing condition
            if e.code == 409:
                body = e.read()
                msg = body.decode("utf-8")
                print(f"Error: {msg}", file=sys.stderr)
                sys.exit(1)
            else:
                print(e, file=sys.stderr)
        except Exception as e:
            print(e, file=sys.stderr)
            pass
        else:
            # OK!
            if status_code == 200:
                return

            # Explicitly disabled
            if status_code == 204:
                print_disabled_block_step("The release-train for this chart has been disabled via the "
                                          "chart-dashboard. Continue?")
                return

        print(
            "Failed to contact chart-dashboard will retry again in 30s",
            file=sys.stderr,
        )
        time.sleep(30)

    # Failed to contact the chart dashboard
    print_disabled_block_step("The release-train for this chart has been disabled via the chart-dashboard. Continue?")
    return


def url_status(url):
    r = urllib.request.urlopen(url)
    _ = r.read()
    status_code = r.getcode()
    return status_code


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


# def download_chart_yaml(repo, sha1, chart):
#     url = (
#         "https://raw.githubusercontent.com/tink-ab/"
#         + "{}/{}/.charts/{}/Chart.yaml".format(repo, sha1, chart)
#     )
#     request = urllib.request.Request(url)
#     auth = read_gh_creds()
#     request.add_header("Authorization", auth)
#     return urllib.request.urlopen(request).read()


# def read_gh_creds():
#     if path.exists("/credentials/git/token"):
#         with open("/credentials/git/token", "r") as token_file:
#             token = token_file.read()
#             auth = "Bearer %s" % token
#     else:
#         with open("/credentials/git/username", "r") as uname:
#             username = uname.read()
#         with open("/credentials/git/password", "r") as pword:
#             password = pword.read()
#         auth = "Basic %s" % base64.b64encode(
#             "{}:{}".format(username, password).encode("utf-8")
#         )
#     return auth


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
    deploy_to_aggregation,
    deploy_to_on_prem,
    deploy_to_global,
    deploy_to_oxford_testing,
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

            # Skip aggregation
            if not deploy_to_aggregation and combined_name in AGGREGATION_CLUSTERS:
                continue

            # Skip global
            if not deploy_to_global and combined_name == GLOBAL_PRODUCTION:
                continue

            # Skip oxford-testing
            if not deploy_to_oxford_testing and combined_name == OXFORD_TESTING:
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


# def filter_clusters_in_freeze(
#     cluster_groups, date=datetime.datetime.now().strftime("%Y-%m-%d")
# ):
#     return [
#         cg
#         for cg in [
#             [
#                 cluster
#                 for cluster in cluster_group
#                 if not skip_release_filter(cluster, date)
#             ]
#             for cluster_group in cluster_groups
#         ]
#         if cg != []
#     ]


# def skip_release_filter(cluster, date):
#     if cluster["cluster"] == "cornwall" and is_seb_restricted_date(date):
#         return True
#     if cluster.get("hotfix_only"):
#         return True
#     return False


def create_annotations(context, style, message):
    run_command(
        ["buildkite-agent", "annotate", message, "--style", style, "--context", context]
    )


def run_command(command):
    subprocess.run(command)


if __name__ == "__main__":
    sys.exit(main())

