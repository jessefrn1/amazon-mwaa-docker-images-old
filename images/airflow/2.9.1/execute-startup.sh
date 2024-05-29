#!/usr/bin/env bash
echo "Executing startup script"

handle_exit() {
	echo "Exiting startup script"
	jq -n env > customer_stored_env.json

	unalias exit
	exit
}

shopt -s expand_aliases # Enable alias expansion (off by default in noninteractive shells)
alias exit=handle_exit # Alias exit to our custom handler to save env vars

# shellcheck disable=SC1091
source "/usr/local/airflow/startup/startup.sh"

echo "Running verification"
bash /usr/local/airflow/verification.sh
echo "Verification completed"

handle_exit