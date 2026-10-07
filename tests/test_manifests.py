"""The Kubernetes manifests, rendered with kustomize, checked for the properties the design relies on."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(shutil.which("kubectl") is None, reason="kubectl (kustomize) not installed")

ENVS = {"qa": "onboarding-qa", "prod": "onboarding-prod"}
SECRETS = {"pg-secret", "app-secret", "langfuse-keys"}


def render(env: str) -> list[dict]:
    out = subprocess.run(  # noqa: S603
        ["kubectl", "kustomize", f"k8s/{env}"],  # noqa: S607 - a fixed command, run on a fixed path
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def of(docs: list[dict], kind: str) -> list[dict]:
    return [d for d in docs if d["kind"] == kind]


def pod_specs(docs: list[dict]) -> list[tuple[str, dict]]:
    return [
        (d["metadata"]["name"], d["spec"]["template"]["spec"])
        for d in docs
        if d["kind"] in {"Deployment", "StatefulSet"}
    ]


def containers(spec: dict) -> list[dict]:
    return [*spec.get("initContainers", []), *spec["containers"]]


@pytest.fixture(scope="module", params=sorted(ENVS))
def env_docs(request):
    return request.param, render(request.param)


def test_everything_lands_in_the_environments_own_namespace(env_docs):
    env, docs = env_docs
    for d in docs:
        assert d["metadata"].get("namespace") == ENVS[env], (d["kind"], d["metadata"]["name"])


def test_expected_resources_exist(env_docs):
    _, docs = env_docs
    kinds = {(d["kind"], d["metadata"]["name"]) for d in docs}
    for expected in [
        ("Deployment", "onboarding-api"),
        ("Deployment", "onboarding-mock-bank"),
        ("StatefulSet", "postgres"),
        ("Service", "onboarding-api"),
        ("Service", "onboarding-mock-bank"),
        ("Service", "postgres"),
        ("Ingress", "onboarding-ingress"),
        ("SecretStore", "aws-secretsmanager"),
        ("ConfigMap", "postgres-init"),
    ]:
        assert expected in kinds, expected
    assert {n for k, n in kinds if k == "ExternalSecret"} == SECRETS
    assert len(of(docs, "NetworkPolicy")) == 4


def test_every_container_is_locked_down_and_bounded(env_docs):
    _, docs = env_docs
    for name, spec in pod_specs(docs):
        assert spec["securityContext"]["runAsNonRoot"] is True, name
        assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault", name
        assert spec["automountServiceAccountToken"] is False, name
        for c in containers(spec):
            sc = c["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False and sc["readOnlyRootFilesystem"] is True, (
                name,
                c["name"],
            )
            assert sc["capabilities"]["drop"] == ["ALL"], (name, c["name"])
            assert {"cpu", "memory"} <= set(c["resources"]["requests"]) and {"cpu", "memory"} <= set(
                c["resources"]["limits"]
            ), (name, c["name"])
            assert (
                "privileged" not in sc
                and "hostNetwork" not in spec
                and "hostPath" not in str(spec.get("volumes", ""))
            )


def test_images_are_never_latest_and_the_app_image_is_rewritable(env_docs):
    env, docs = env_docs
    for name, spec in pod_specs(docs):
        for c in containers(spec):
            image = c["image"]
            assert not image.endswith(":latest") and ":" in image, (name, image)
            if name != "postgres":
                assert image.startswith("client-onboarding:"), (
                    image
                )  # kustomize `images:` rewrote the placeholder
    pg = next(s for n, s in pod_specs(docs) if n == "postgres")
    assert pg["containers"][0]["image"] == "postgres:16.6"  # pinned, not a floating tag


def test_the_api_runs_as_its_irsa_account_in_the_right_mode(env_docs):
    env, docs = env_docs
    api = next(d for d in of(docs, "Deployment") if d["metadata"]["name"] == "onboarding-api")
    spec = api["spec"]["template"]["spec"]
    assert spec["serviceAccountName"] == "onboarding-api"
    envs = {e["name"]: e for e in next(c for c in spec["containers"] if c["name"] == "api")["env"]}
    assert envs["ENVIRONMENT"]["value"] == env
    assert envs["KYC_BASE_URL"]["value"] == f"http://nodejs-service.{env}.svc.cluster.local"
    assert envs["LLM_BACKEND"]["value"] == "bedrock" and envs["LLM_ENABLED"]["value"] == "true"
    assert envs["BEDROCK_MODEL_ID"]["value"].startswith("in.anthropic.")  # India-only profile, never global.
    assert envs["MOCK_BANK_URL"]["value"] == "http://onboarding-mock-bank"
    # tokens and session secret are required (the pod fails closed); Langfuse and the KYC key are optional
    for required in ("ONBOARDING_TOKENS", "SESSION_SECRET", "ONBOARDING_APP_PASSWORD"):
        assert "optional" not in envs[required]["valueFrom"]["secretKeyRef"], required
    for optional in ("KYC_API_KEY", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        assert envs[optional]["valueFrom"]["secretKeyRef"]["optional"] is True, optional
    assert "fake" not in str(envs.values()).lower()


def test_the_migrate_init_container_uses_the_owner_role_and_the_app_does_not(env_docs):
    _, docs = env_docs
    api = next(d for d in of(docs, "Deployment") if d["metadata"]["name"] == "onboarding-api")
    spec = api["spec"]["template"]["spec"]
    init = spec["initContainers"][0]
    assert init["name"] == "migrate" and "onboarding.db migrate" in init["command"][-1]
    assert any(e["name"] == "OWNER_DATABASE_URL" for e in init["env"])
    main_env = {e["name"] for e in next(c for c in spec["containers"] if c["name"] == "api")["env"]}
    assert "OWNER_DATABASE_URL" not in main_env and "ONBOARDING_OWNER_PASSWORD" not in main_env
    assert api["spec"]["replicas"] == 1 and api["spec"]["strategy"]["rollingUpdate"]["maxUnavailable"] == 0


def test_every_secret_the_pods_read_is_produced_by_an_external_secret(env_docs):
    env, docs = env_docs
    refs = set()
    for _, spec in pod_specs(docs):
        for c in containers(spec):
            for e in c.get("env", []):
                ref = e.get("valueFrom", {}).get("secretKeyRef")
                if ref:
                    refs.add(ref["name"])
    assert refs <= SECRETS, refs - SECRETS
    keys = {d["spec"]["dataFrom"][0]["extract"]["key"] for d in of(docs, "ExternalSecret")}
    assert keys == {f"{env}/onboarding/{s}" for s in SECRETS}  # each environment reads only its own secrets


def test_the_manifests_contain_no_secret_values(env_docs):
    _, docs = env_docs
    assert not of(docs, "Secret")
    text = yaml.safe_dump(docs)
    for forbidden in ("PASSWORD: ", "password=", "BEGIN PRIVATE KEY", "AKIA"):
        assert forbidden not in text, forbidden


def test_postgres_storage_differs_by_environment(env_docs):
    env, docs = env_docs
    sts = next(d for d in of(docs, "StatefulSet") if d["metadata"]["name"] == "postgres")
    claim = sts["spec"]["volumeClaimTemplates"][0]["spec"]
    assert claim["storageClassName"] == (
        "ebs-sc" if env == "qa" else "ebs-sc-retain"
    )  # prod data survives a PVC deletion
    assert claim["resources"]["requests"]["storage"] == ("5Gi" if env == "qa" else "10Gi")
    init = next(d for d in of(docs, "ConfigMap") if d["metadata"]["name"] == "postgres-init")
    assert (
        "PASSWORD :'" in init["data"]["init.sh"] and "CREATE ROLE onboarding_app" in init["data"]["init.sh"]
    )


def test_ingress_joins_the_shared_alb_with_this_projects_hostname(env_docs):
    env, docs = env_docs
    ing = of(docs, "Ingress")[0]
    host = "qa-proj4-onboarding.bolarbrijesh.com" if env == "qa" else "proj4-onboarding.bolarbrijesh.com"
    ann = ing["metadata"]["annotations"]
    assert ann["alb.ingress.kubernetes.io/group.name"] == "devsecops-shared"
    assert (
        ann["alb.ingress.kubernetes.io/healthcheck-path"] == "/healthz"
        and ann["alb.ingress.kubernetes.io/ssl-redirect"] == "443"
    )
    assert "alb.ingress.kubernetes.io/certificate-arn" not in ann
    assert ing["spec"]["rules"][0]["host"] == host and ing["spec"]["tls"][0]["hosts"] == [host]
    assert ing["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["name"] == "onboarding-api"


def test_the_mock_bank_and_database_are_not_exposed(env_docs):
    _, docs = env_docs
    policies = {d["metadata"]["name"]: d for d in of(docs, "NetworkPolicy")}
    assert policies["default-deny-ingress"]["spec"]["podSelector"] == {}
    bank = policies["allow-mock-bank-from-api"]["spec"]["ingress"][0]["from"][0]["podSelector"]["matchLabels"]
    assert bank == {"app": "onboarding-api"}
    pg_from = [
        f["podSelector"]["matchLabels"]["app"]
        for f in policies["allow-postgres-from-services"]["spec"]["ingress"][0]["from"]
    ]
    assert sorted(pg_from) == ["onboarding-api", "onboarding-mock-bank"]
    assert [i["metadata"]["name"] for i in of(docs, "Ingress")] == ["onboarding-ingress"]
    backends = {
        p["backend"]["service"]["name"]
        for i in of(docs, "Ingress")
        for r in i["spec"]["rules"]
        for p in r["http"]["paths"]
    }
    assert backends == {"onboarding-api"}


def test_prod_and_qa_differ_only_where_intended():
    qa, prod = render("qa"), render("prod")
    assert {(d["kind"], d["metadata"]["name"]) for d in qa} == {
        (d["kind"], d["metadata"]["name"]) for d in prod
    }


def test_the_checked_in_qa_overlay_only_records_a_deployed_tag():
    text = Path("k8s/qa/kustomization.yaml").read_text()
    assert "newTag:" in text and "newName: client-onboarding" in text


def test_the_init_script_is_executable_and_free_of_secrets():
    p = Path("k8s/base/postgres-init.sh")
    assert p.stat().st_mode & 0o111, "the postgres image runs init scripts that are executable"
    assert "PASSWORD '" not in p.read_text().replace(":'", "")


def test_the_api_probes_tolerate_a_busy_process(env_docs):
    """A restart in the middle of a request is worse than a slow probe (the first QA deploy restarted the pod mid-case)."""
    _, docs = env_docs
    api = next(d for d in of(docs, "Deployment") if d["metadata"]["name"] == "onboarding-api")
    c = next(c for c in api["spec"]["template"]["spec"]["containers"] if c["name"] == "api")
    live = c["livenessProbe"]
    assert live["timeoutSeconds"] >= 5 and live["periodSeconds"] * live["failureThreshold"] >= 120
    assert c["readinessProbe"]["timeoutSeconds"] >= 5


def test_documents_go_to_s3_and_the_bucket_name_is_filled_in_by_the_pipeline(env_docs):
    env, docs = env_docs
    api = next(d for d in of(docs, "Deployment") if d["metadata"]["name"] == "onboarding-api")
    envs = {e["name"]: e.get("value") for e in next(c for c in api["spec"]["template"]["spec"]["containers"] if c["name"] == "api")["env"]}
    assert envs["DOCUMENT_STORE"] == "s3" and envs["DOCUMENT_BUCKET"] == "set-by-pipeline"
    workflow = Path(".github/workflows/qa-cicd.yml" if env == "qa" else ".github/workflows/prod-cd.yaml").read_text()
    assert f"client-onboarding-docs-$ACCOUNT-{env}-$AWS_REGION" in workflow and "set-by-pipeline" in workflow
