from datetime import datetime, timedelta

from airflow import DAG
from airflow.operators.bash import BashOperator

default_args = {
    'owner': 'airflow',
    'depends_on_past': False,
    'email_on_failure': False,
    'email_on_retry': False,
    'retries': 1,
    'retry_delay': timedelta(minutes=5),
}

with DAG(
    dag_id='simple_bash_hello_world',
    default_args=default_args,
    description='A simple bash workflow that prints Hello World',
    schedule_interval=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=['sample', 'bash'],
) as dag:

    hello_world_task = BashOperator(
        task_id='say_hello',
        bash_command='echo "Hello World!"',
    )

    hello_world_task