// Declarative Jenkins pipeline for the Docker image update automation.
// Agent needs: python3 (3.15 recommended, 3.11+ supported), git, and docker (only for build validation) on Linux.
//
// Token credentials: map the ENVIRONMENT VARIABLE NAME the tool expects (GIT_TOKEN, GIT_TOKEN2, ...) to the ID of a Jenkins
// "Secret text" credential. The Excel column Token_Name refers to these names. Add one line per extra token.
// List only credentials that really exist (a missing ID makes withCredentials fail).
def GIT_TOKEN_CREDENTIALS = [
    GIT_TOKEN : 'github-token-1',
    GIT_TOKEN2: 'github-token-2',
    // GIT_TOKEN3: 'github-token-3',
]

pipeline {
    agent any

    options {
        timestamps()
        disableConcurrentBuilds()
        timeout(time: 6, unit: 'HOURS')
        buildDiscarder(logRotator(numToKeepStr: '30', artifactNumToKeepStr: '30'))
    }

    // Uncomment for a nightly run (H spreads the load):
    // triggers { cron('H 2 * * *') }

    parameters {
        string(name: 'REPOSITORIES', defaultValue: '', description: 'Repo_Name values to process, comma separated. Empty = ALL rows of the Excel file')
        choice(name: 'UPGRADE_STRATEGY', choices: ['same_major', 'new_major', 'both'], description: 'Strategy to evaluate')
        booleanParam(name: 'DRY_RUN', defaultValue: true, description: 'Evaluate and report only - never modify a Dockerfile')
        booleanParam(name: 'CREATE_PR', defaultValue: false, description: 'Commit to a new branch and open pull requests (needs DRY_RUN=false and write-capable tokens)')
        booleanParam(name: 'TEST_MODE', defaultValue: true, description: 'TEST MODE: run everything but do NOT send the e-mail and do NOT create pull requests')
        booleanParam(name: 'SEND_EMAIL', defaultValue: true, description: 'Send the consolidated e-mail (ignored while TEST_MODE is on)')
        string(name: 'INVENTORY_FILE', defaultValue: 'config/repositories.xlsx', description: 'Excel inventory, relative to the workspace')
    }

    environment {
        PYTHONUNBUFFERED           = '1'
        DIU_STRATEGY               = "${params.UPGRADE_STRATEGY}"
        DIU_DRY_RUN                = "${params.DRY_RUN}"
        DIU_REPORT_ONLY            = "${params.DRY_RUN}"
        DIU_CREATE_PR              = "${params.CREATE_PR && !params.DRY_RUN}"
        DIU_TEST_MODE              = "${params.TEST_MODE}"
        DIU_EMAIL_ENABLED          = "${params.SEND_EMAIL}"
        DIU_REPOS_FILE             = "${params.INVENTORY_FILE}"
        // The central report folder lives in the WORKSPACE root; the tool creates it when it is missing
        DIU_DOCKERFILE_REPORTS_DIR = 'dockerfile-reports'
    }

    stages {
        stage('Validate parameters') {
            steps {
                script {
                    if (params.REPOSITORIES && !(params.REPOSITORIES ==~ /^[A-Za-z0-9._, -]*$/)) {
                        error 'REPOSITORIES contains unsupported characters'
                    }
                    if (!(params.INVENTORY_FILE ==~ /^[A-Za-z0-9._\/-]+$/) || params.INVENTORY_FILE.contains('..')) {
                        error 'INVENTORY_FILE must be a relative path inside the workspace'
                    }
                }
            }
        }

        stage('Prepare') {
            steps {
                // start from clean report folders so the artifacts only contain THIS build's reports
                sh '''
                    set -eu
                    rm -rf dockerfile-reports reports repos
                    mkdir -p dockerfile-reports reports
                    # use the newest interpreter the agent has (python3.15 -> python3.14 -> python3)
                    PY=$(command -v python3.15 || command -v python3.14 || command -v python3)
                    "$PY" -m venv .venv
                    . .venv/bin/activate
                    python -m pip install --quiet --upgrade pip
                    python -m pip install --quiet -r requirements-dev.txt
                    python --version
                    python -c "import sys; assert sys.version_info >= (3, 11), 'Python 3.11+ required (3.15 recommended)'"
                    git --version
                '''
            }
        }

        stage('Self-tests') {
            steps {
                sh '. .venv/bin/activate && python -m pytest -q'
            }
        }

        stage('Run Docker image update') {
            steps {
                script {
                    // One binding per configured token -> GIT_TOKEN, GIT_TOKEN2, ... (values are masked in the console log)
                    def bindings = GIT_TOKEN_CREDENTIALS.collect { envName, credId ->
                        string(credentialsId: credId, variable: envName)
                    }
                    bindings << usernamePassword(credentialsId: 'smtp-credentials', usernameVariable: 'SMTP_USERNAME', passwordVariable: 'SMTP_PASSWORD')
                    withCredentials(bindings) {
                        withEnv(["DIU_REPOSITORIES=${params.REPOSITORIES}",
                                 "SMTP_SERVER=${env.SMTP_SERVER ?: 'smtp.example.com'}",
                                 "SMTP_PORT=${env.SMTP_PORT ?: '587'}",
                                 "EMAIL_FROM=${env.EMAIL_FROM ?: 'devops-automation@example.com'}",
                                 "SCHEDULED_TIME=Jenkins ${env.JOB_NAME} #${env.BUILD_NUMBER}"]) {
                            // A repository that fails (no access, no Dockerfile, no report) never stops the job.
                            // Exit code 1 = "some repositories failed" -> build is UNSTABLE, not FAILED.
                            def rc = sh(returnStatus: true, script: '. .venv/bin/activate && python -m src.main --config config/settings.yaml')
                            if (rc == 1) {
                                unstable('Some repositories failed - see reports/overall-summary.txt and dockerfile-reports/')
                            } else if (rc != 0) {
                                error "Docker image update crashed or was misconfigured (exit code ${rc})"
                            }
                        }
                    }
                }
            }
        }

        stage('Collect reports') {
            steps {
                // Reports are already centralised by the tool ({repo-name}-{DD-Mon-YYYY}.txt). Verify only - never fail.
                sh '''
                    mkdir -p dockerfile-reports
                    echo "Reports in dockerfile-reports/:"
                    ls -1 dockerfile-reports || true
                    echo "Count: $(find dockerfile-reports -maxdepth 1 -name '*.txt' | wc -l)"
                '''
            }
        }
    }

    post {
        always {
            // allowEmptyArchive: a build in which no repository produced a report must not fail at this step
            archiveArtifacts artifacts: 'dockerfile-reports/**/*.txt, reports/*.csv, reports/*.zip, reports/overall-summary.txt, reports/email-recipient-summary.txt, reports/email-preview-NOT-SENT.eml, reports/logs/**',
                             allowEmptyArchive: true, fingerprint: true
        }
        cleanup {
            // safety net: the tool already empties repos/<repo-name>/ after the summary e-mail; this also covers an aborted build
            sh 'rm -rf .venv repos'
        }
    }
}
