import base64
import json
import logging
import random
import re
import shutil
import string
import tempfile
import traceback
import zlib
from os import environ, path
from subprocess import DEVNULL, CalledProcessError, check_call
from time import sleep
from typing import List, Optional
from urllib.parse import unquote

import connexion
import cwltool.load_tool as load  # TODO: use my functions instead
import psutil
from airflow.models import DagBag, DagModel, DagRun, TaskInstance
from airflow.settings import AIRFLOW_HOME, DAGS_FOLDER
from airflow.utils.db import provide_session
from airflow.utils.state import State
from airflow.utils.timezone import parse as parsedate
from cwl_airflow.utilities.cwl import (DAG_TEMPLATE, conf_get,
                                       convert_to_workflow, fast_cwl_load,
                                       slow_cwl_load)
from cwl_airflow.utilities.helpers import (get_compressed, get_dir,
                                           get_md5_sum, get_version)
from schema_salad.ref_resolver import Loader
from six import iterlists
from werkzeug.utils import secure_filename


class CWLApiBackend:

    # curl -X GET "127.0.0.1:8081/wes/v1/dags" -H "accept: application/json"
    # curl -X GET "127.0.0.1:8081/wes/v1/dags?dag_ids=example_bash_operator" -H "accept: application/json"
    # curl -X GET "127.0.0.1:8081/wes/v1/dags?dag_ids=example_bash_operator,tutorial" -H "accept: application/json"

    # curl -X GET "127.0.0.1:8081/wes/v1/dag_runs?dag_id=example_bash_operator&state=running" -H "accept: application/json"
    # curl -X GET "127.0.0.1:8081/wes/v1/dag_runs?dag_id=example_bash_operator&state=success" -H "accept: application/json"
    # curl -X GET "127.0.0.1:8081/wes/v1/dag_runs?dag_id=example_bash_operator" -H "accept: application/json"
    # curl -X GET "127.0.0.1:8081/wes/v1/dag_runs?state=running" -H "accept: application/json"
    # curl -X GET "127.0.0.1:8081/wes/v1/dag_runs" -H "accept: application/json"

    # curl -X GET "127.0.0.1:8081/wes/v1/dag_runs?run_id=scheduled__2019-07-20T00%3A00%3A00%2B00%3A00" -H "accept: application/json"
    # curl -X GET "127.0.0.1:8081/wes/v1/dag_runs?run_id=scheduled__2019-07-20T00%3A00%3A00%2B00%3A00&dag_id=tutorial" -H "accept: application/json"
    # curl -X GET "127.0.0.1:8081/wes/v1/dag_runs?execution_date=2019-07-20T00%3A00%3A00%2B00%3A00" -H "accept: application/json"

    # curl -X POST "127.0.0.1:8081/wes/v1/dag_runs?dag_id=example_bash_operator&run_id=1234567&conf=%22%7B%7D%22" -H "accept: application/json"
    # curl -X POST "localhost:8081/wes/v1/dags?dag_id=bowtie2-indices" -H "accept: application/json" -H "Content-Type: multipart/form-data" -F "workflow=@bowtie2-indices.cwl"
    # curl -X POST "localhost:8081/wes/v1/dags/bowtie2-indices/dag_runs?run_id=bowtie2_indices_1&conf=%22%7B%7D%22" -H "accept: application/json"

    # curl -X GET "127.0.0.1:8081/api/experimental/wes/service-info" -H "accept: application/json"
    # curl -X GET "127.0.0.1:8081/api/experimental/wes/runs" -H "accept: application/json"
    # curl -X POST "127.0.0.1:8081/api/experimental/wes/runs/pracfcizfvmhdefxqdomtxktkbflhgav/cancel" -H "accept: application/json"

    # curl -X GET "127.0.0.1:8081/api/experimental/wes/runs/zlqukumkxxfkumrevclzjcsbyuguhwqy" -H "accept: application/json"
    # curl -X GET "127.0.0.1:8081/api/experimental/wes/runs/pracfcizfvmhdefxqdomtxktkbflhgav/status" -H "accept: application/json"
    # curl -X POST "127.0.0.1:8081/api/experimental/wes/runs" -H "accept: application/json" -H "Content-Type: multipart/form-data" -F "workflow_attachment[]=@custom-bash.cwl"

    def __init__(self):
        get_dir(DAGS_FOLDER)
        self.include_examples = False
        self.dag_template_with_tmp_folder = "#!/usr/bin/env python3\nfrom cwl_airflow import CWLDAG, CWLJobDispatcher, CWLJobGatherer\ndag = CWLDAG(cwl_workflow='{0}', dag_id='{1}', default_args={{'tmp_folder':'{2}'}})\ndag.create()\ndag.add(CWLJobDispatcher(dag=dag), to='top')\ndag.add(CWLJobGatherer(dag=dag), to='bottom')"
        self.wes_state_conversion = {
            "running": "RUNNING",
            "success": "COMPLETE",
            "failed": "EXECUTOR_ERROR",
        }
        self.validated_dags = (
            {}
        )  # stores dags' content md5 checksums as keys and one of the statuses ["checking", "success", "error"] as values

    def get_cwl(self, dag_id: str):
        logging.debug(f"Call get_cwl_path with dag_id={dag_id}")
        cwl_path = path.join(DAGS_FOLDER, dag_id + ".cwl")
        try:
            if not path.exists(cwl_path):
                return connexion.problem(404, f"Dag {dag_id} not exists", "")
            with open(cwl_path, "rb") as f:
                content = f.read()
            compressed = base64.b64encode(zlib.compress(content, level=9)).decode(
                "utf-8"
            )
            return compressed
        except Exception as err:
            logging.error(f"Failed while running get_cwl_path {err}")
            return connexion.problem(500, "Failed to read cwl path", str(err))

    def get_dags(
        self,
        dag_ids=[],
        match: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ):
        logging.debug(f"Call get_dags with dag_ids={dag_ids}")
        try:
            dag_bag = DagBag(include_examples=self.include_examples)
            dag_ids = dag_ids or self.list_dags(
                dag_bag=dag_bag, match=match, sort=True, limit=limit, offset=offset
            )
            logging.debug(f"Processing dags {dag_ids}")
            return {
                "dags": [
                    {
                        "dag_id": dag_id,
                        "tasks": self.list_tasks(dag_id, dag_bag=dag_bag),
                    }
                    for dag_id in dag_ids
                ]
            }
        except Exception as err:
            logging.error(f"Failed while running get_dags {err}")
            return {"dags": []}

    def get_dags_count(self, match: Optional[str] = None) -> int:
        logging.debug(f"Call get_dags_count with match={match}")
        try:
            dag_bag = DagBag(include_examples=self.include_examples)
            dag_ids = self.list_dags(dag_bag=dag_bag, match=match)
            return len(dag_ids)
        except Exception as err:
            logging.error(f"Failed while running get_dags_count {err}")
            return 0

    def post_dag(self, dag_id=None):
        logging.debug(f"Call post_dag with dag_id={dag_id}")
        try:
            res = self.export_dag(
                dag_id
                or "".join(random.choice(string.ascii_lowercase) for i in range(32))
            )
            logging.debug(f"Exported DAG {res}")
            return res
        except Exception as err:
            logging.error(f"Failed while running post_dag {err}")
            return connexion.problem(500, "Failed to create dag", str(err))

    def get_dag_runs(
        self,
        dag_id=None,
        run_id=None,
        execution_date=None,
        state=None,
        match: Optional[str] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ):
        logging.debug(
            f"Call get_dag_runs with dag_id={dag_id}, "
            f"run_id={run_id}, execution_date={execution_date}, state={state}, "
            f"match={match}, limit={limit}, offset={offset}"
        )
        if run_id:
            run_id = unquote(run_id)
        if dag_id:
            dag_id = unquote(dag_id)
        if execution_date:
            execution_date = unquote(execution_date)
        try:
            dag_runs = []
            dag_bag = DagBag(include_examples=self.include_examples)
            dag_ids = (
                [dag_id]
                if dag_id
                else self.list_dags(dag_bag=dag_bag, match=match, sort=True)
            )
            logging.debug(f"Processing dags {dag_ids}")
            num_skipped = 0
            done = False
            for d_id in dag_ids:
                logging.debug(f"Process dag  {d_id}")
                task_ids = self.list_tasks(d_id, dag_bag=dag_bag)
                logging.debug(f"Fetched tasks {task_ids}")
                for dag_run in self.list_dag_runs(d_id, state, run_id=run_id):
                    if len(dag_runs) == limit:
                        done = True
                        break

                    logging.debug(
                        f"Process dag run {dag_run['run_id']}, {dag_run['execution_date']}"
                    )
                    if (
                        run_id
                        and run_id != dag_run["run_id"]
                        or execution_date
                        and execution_date != dag_run["execution_date"]
                    ):
                        logging.debug(
                            f"Skip dag_run {dag_run['run_id']}, {dag_run['execution_date']} (run_id or execution_date doesn't match)"
                        )
                        continue
                    response_item = {
                        "dag_id": d_id,
                        "run_id": dag_run["run_id"],
                        "execution_date": dag_run["execution_date"],
                        "start_date": dag_run["start_date"],
                        "state": dag_run["state"],
                        "tasks": [],
                        "conf": json.dumps(
                            dag_run["conf"], indent=2, ensure_ascii=False
                        ),
                    }
                    logging.debug(f"Get statuses for tasks {task_ids}")
                    for t_id in task_ids:
                        response_item["tasks"].append(
                            {
                                "id": t_id,
                                "state": self.task_state(
                                    d_id,
                                    t_id,
                                    dag_run["execution_date"],
                                    dag_bag=dag_bag,
                                ),
                            }
                        )
                    if offset is not None and num_skipped < offset:
                        num_skipped += 1
                        continue

                    dag_runs.append(response_item)
                if done:
                    break
            return {"dag_runs": dag_runs}
        except Exception as err:
            traceback.print_exc()
            logging.error(f"Failed to call get_dag_runs {err}")
            return {"dag_runs": []}

    def get_dag_runs_count(self, match: Optional[str] = None) -> int:
        logging.debug(f"Call get_dag_runs_count with match={match}")
        try:
            dag_bag = DagBag(include_examples=self.include_examples)
            dag_ids = self.list_dags(dag_bag=dag_bag, match=match)
            return self.count_dag_runs(dag_ids=list(dag_ids))
        except Exception as err:
            logging.error(f"Failed while running get_dag_runs_count {err}")
            return 0

    def post_dag_runs(self, dag_id, run_id=None, conf=None):
        logging.debug(
            f"Call post_dag_runs with dag_id={dag_id}, run_id={run_id}, conf={conf}"
        )
        try:
            dagrun = self.trigger_dag(dag_id, run_id, conf)
            return {
                "dag_id": dagrun.dag_id,
                "run_id": dagrun.run_id,
                "execution_date": dagrun.execution_date,
                "start_date": dagrun.start_date,
                "state": dagrun.state,
            }
        except Exception as err:
            logging.error(f"Failed to call post_dag_runs {err}")
            return connexion.problem(500, "Failed to create dag_run", str(err))

    def post_dag_runs_legacy(self, dag_id):
        data = connexion.request.json
        logging.debug(f"Call post_dag_runs_legacy with dag_id={dag_id}, data={data}")
        return self.post_dag_runs(dag_id, data["run_id"], data["conf"])

    def wait_until_dag_validated(self, dag_path):
        """
        Reads the md5 sum of the DAG python file to see whether it
        was updated. Searches by md5 sum if exactly the same DAG
        python file has been already validated. If it was already
        validated raises exception in case of "error" result
        or does nothing if validation was successfull.
        If validation check for the specific DAG python file is
        still running ("checking" status), sleeps for 1 second and
        checks status again. This approach prevents from running
        multiple processes for exactly the same DAG python file on
        each POST request. Instead of using "airflow list_dags -sd"
        that never ends with exit code other than 0, we use "python3".
        Environment is copied to the subprocess, so it should work
        fine even in portable CWL-Airflow installation
        """
        dag_md5_sum = get_md5_sum(dag_path)
        if dag_md5_sum not in self.validated_dags:
            self.validated_dags[dag_md5_sum] = "checking"
            try:
                check_call(
                    ["python3", dag_path],
                    env=environ.copy(),
                    stdout=DEVNULL,
                    stderr=DEVNULL,
                )
                self.validated_dags[dag_md5_sum] = "success"
            except CalledProcessError:
                self.validated_dags[dag_md5_sum] = "error"
        while self.validated_dags[dag_md5_sum] not in ["success", "error"]:
            sleep(1)
        if self.validated_dags[dag_md5_sum] == "error":
            raise ValueError(f"Failed to load DAG from {dag_path}")

    @provide_session
    def create_dag_run(self, dag_id, run_id, conf, session):
        """
        Creates new DagRun. Shouldn't be called with not existent dag_id.
        Raises exception if DagRun for the same DAG with the same run_id
        was previously created
        """
        if (
            session.query(DagRun)
            .filter(DagRun.dag_id == dag_id, DagRun.run_id == run_id)
            .one_or_none()
        ):
            raise ValueError(f"dag_run {run_id} for dag_id {dag_id} already exists")
        else:
            run_conf = conf if isinstance(conf, dict) else json.loads(conf)
            dag_run = DagRun(
                dag_id=dag_id, run_id=run_id, conf=run_conf, run_type="cwl"
            )
            session.add(dag_run)
            session.commit()
            return dag_run

    @provide_session
    def trigger_dag(self, dag_id, run_id, conf, session):
        """
        Checks if DAG exists (DAG python file was already parsed), then
        creates new DagRun with that DAG.
        If DAG doesn't exist, checks if DAG python file can be loaded,
        waits untill it's validated and only after that creates new DagRun.
        """
        if session.query(DagModel).filter(DagModel.dag_id == dag_id).one_or_none():
            return self.create_dag_run(dag_id, run_id, conf)
        else:
            self.wait_until_dag_validated(path.join(DAGS_FOLDER, dag_id + ".py"))
            return self.create_dag_run(dag_id, run_id, conf)

    def list_dags(
        self,
        dag_bag: Optional[DagBag] = None,
        match: Optional[str] = None,
        sort=False,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
    ):
        if not dag_bag:
            dag_bag = DagBag(include_examples=self.include_examples)
        if match is None:
            ids = list(dag_bag.dags.keys())
        else:
            pattern = re.compile(match)
            ids = [x for x in dag_bag.dags.keys() if re.match(pattern, x)]
        if sort:
            ids.sort()

        if offset is not None:
            ids = ids[offset:]
        if limit is not None:
            ids = ids[:limit]

        return ids

    def list_tasks(self, dag_id, dag_bag: Optional[DagBag] = None):
        if not dag_bag:
            dag_bag = DagBag(include_examples=self.include_examples)
        return [t.task_id for t in dag_bag.dags[dag_id].tasks]

    def task_state(
        self, dag_id, task_id, execution_date, dag_bag: Optional[DagBag] = None
    ):
        if not dag_bag:
            dag_bag = DagBag(include_examples=self.include_examples)
        task_state = TaskInstance(
            dag_bag.dags[dag_id].get_task(task_id=task_id), parsedate(execution_date)
        ).current_state()
        task_state = task_state or "none"
        return task_state

    def list_dag_runs(self, dag_id, state, run_id=None):
        dag_runs = []
        for dag_run in self.find_dag_runs(dag_id=dag_id, state=state, run_id=run_id):
            dag_runs.append(
                {
                    "run_id": dag_run.run_id,
                    "state": dag_run.state,
                    "execution_date": dag_run.execution_date.isoformat(),
                    "start_date": (
                        (dag_run.start_date or "") and dag_run.start_date.isoformat()
                    ),
                    "conf": dag_run.conf,
                }
            )
        return dag_runs

    @staticmethod
    @provide_session
    def find_dag_runs(
        dag_id=None,
        run_id=None,
        execution_date=None,
        state=None,
        external_trigger=None,
        no_backfills=False,
        session=None,
    ):
        """
        Copied from `airflow.DagRun.find` with sort the dag runs by
        execution date in descend.

        Returns a set of dag runs for the given search criteria.

        :param dag_id: the dag_id to find dag runs for
        :type dag_id: int, list
        :param run_id: defines the the run id for this dag run
        :type run_id: str
        :param execution_date: the execution date
        :type execution_date: datetime.datetime
        :param state: the state of the dag run
        :type state: str
        :param external_trigger: whether this dag run is externally triggered
        :type external_trigger: bool
        :param no_backfills: return no backfills (True), return all (False).
            Defaults to False
        :type no_backfills: bool
        :param session: database session
        :type session: sqlalchemy.orm.session.Session
        """
        DR = DagRun

        qry = session.query(DR)
        if dag_id:
            qry = qry.filter(DR.dag_id == dag_id)
        if run_id:
            qry = qry.filter(DR.run_id == run_id)
        if execution_date:
            if isinstance(execution_date, list):
                qry = qry.filter(DR.execution_date.in_(execution_date))
            else:
                qry = qry.filter(DR.execution_date == execution_date)
        if state:
            qry = qry.filter(DR.state == state)
        if external_trigger is not None:
            qry = qry.filter(DR.external_trigger == external_trigger)
        if no_backfills:
            # in order to prevent a circular dependency
            from airflow.jobs import BackfillJob

            qry = qry.filter(DR.run_id.notlike(BackfillJob.ID_PREFIX + "%"))

        dr = qry.order_by(DR.execution_date.desc()).all()

        return dr

    @staticmethod
    @provide_session
    def count_dag_runs(
        dag_ids: Optional[List[str]] = None, state: Optional[str] = None, session=None
    ) -> int:
        DR = DagRun

        qry = session.query(DR)
        if dag_ids:
            qry = qry.filter(DR.dag_id.in_(dag_ids))
        if state:
            qry = qry.filter(DR.state == state)

        return qry.order_by(DR.execution_date).count()

    def save_attachment(self, attachment, location, exist_ok=False):
        if path.isfile(location) and not exist_ok:
            raise FileExistsError(f"File {location} already exist")
        data = connexion.request.files[attachment]
        data.save(location)

    def export_dag(self, dag_id):
        """
        Checks if DAG python file with the same name has been already
        exported. If not, checks if exaclty one of "workflow" and
        "workflow_content" parameters are present in the request. In
        case of "workflow_content" first we need to load a tool from
        it and try to convert it to Workflow (what if it was
        CommandLineTool), then compress it again and write to DAG
        python file. In case of "workflow", first we need to save
        attachment, then try to comvert it to Workflow (the same reason
        as above) and write it to DAG python file.
        """

        dag_path = path.join(DAGS_FOLDER, dag_id + ".py")
        cwl_path = path.join(DAGS_FOLDER, dag_id + ".cwl")

        if path.isfile(dag_path):
            raise FileExistsError(f"File {dag_path} already exist")

        if (
            "workflow_content" in (connexion.request.json or [])
            and "workflow" in connexion.request.files
        ):

            raise ValueError(
                "Only one of the 'workflow' or \
                'workflow_content' parameters can be set"
            )

        if "workflow_content" in (
            connexion.request.json or []
        ):  # json field might be None, need to take [] as default

            with open(cwl_path, "w") as f:
                uncompressed = zlib.decompress(
                    base64.b64decode(
                        connexion.request.json["workflow_content"].encode("utf-8")
                    )
                ).decode("utf-8")
                f.write(uncompressed)

            workflow = get_compressed(
                convert_to_workflow(  # to make sure we are not saving CommandLineTool instead of a Workflow
                    command_line_tool=fast_cwl_load(  # using fast_cwl_load is safe here because we deal with the content of a file
                        connexion.request.json["workflow_content"]
                    )
                )
            )

        elif "workflow" in connexion.request.files:

            workflow = cwl_path
            self.save_attachment("workflow", workflow)
            convert_to_workflow(
                command_line_tool=slow_cwl_load(  # safer to use slow_cwl_load, because of the possible confusions with all these renaming. TODO: make it less complicate
                    workflow=workflow, only_tool=True
                ),
                location=workflow,
            )

        else:
            raise ValueError(
                "At least one of the 'workflow' or \
                'workflow_content' parameters should be set"
            )

        with open(dag_path, "w") as output_stream:
            output_stream.write(DAG_TEMPLATE.format(workflow, dag_id))

        return {"dag_id": dag_id, "cwl_path": workflow, "dag_path": dag_path}

    ###########################################################################
    # WES                                                                     #
    ###########################################################################

    def wes_collect_attachments(self, run_id):
        tempdir = tempfile.mkdtemp(
            dir=get_dir(
                path.abspath(
                    conf_get(
                        "cwl", "tmp_folder", path.join(AIRFLOW_HOME, "cwl_tmp_folder")
                    )
                )
            ),
            prefix="run_id_" + run_id + "_",
        )
        logging.debug(f"Save all attached files to {tempdir}")
        for k, ls in iterlists(connexion.request.files):
            logging.debug(f"Process attachment parameter {k}")
            if k == "workflow_attachment":
                for v in ls:
                    try:
                        logging.debug(f"Process attached file {v}")
                        sp = v.filename.split("/")
                        fn = []
                        for p in sp:
                            if p not in ("", ".", ".."):
                                fn.append(secure_filename(p))
                        dest = path.join(tempdir, *fn)
                        if not path.isdir(path.dirname(dest)):
                            get_dir(path.dirname(dest))
                        logging.debug(f"Save {v.filename} to {dest}")
                        v.save(dest)
                    except Exception as err:
                        raise ValueError(f"Failed to process attached file {v}, {err}")
        body = {}
        for k, ls in iterlists(connexion.request.form):
            logging.debug(f"Process form parameter {k}")
            for v in ls:
                try:
                    if not v:
                        continue
                    if k == "workflow_params":
                        job_file = path.join(tempdir, "job.json")
                        with open(job_file, "w") as f:
                            json.dump(json.loads(v), f, indent=4)
                        logging.debug(f"Save job file to {job_file}")
                        loader = Loader(load.jobloaderctx.copy())
                        job_order_object, _ = loader.resolve_ref(
                            job_file, checklinks=False
                        )
                        body[k] = job_order_object
                    else:
                        body[k] = v
                except Exception as err:
                    raise ValueError(
                        f"Failed to process form parameter {k}, {v}, {err}"
                    )

        if "workflow_params" not in body or "workflow_url" not in body:
            raise ValueError(
                "Missing 'workflow_params' or 'workflow_url' in submission"
            )

        body["workflow_url"] = path.join(tempdir, secure_filename(body["workflow_url"]))

        return tempdir, body

    def wes_get_service_info(self):
        logging.debug(f"Call wes_get_service_info")
        response = {
            "workflow_type_versions": {"CWL": {"workflow_type_version": ["v1.0"]}},
            "supported_wes_versions": ["1.0.0"],
            "supported_filesystem_protocols": ["file"],
            "workflow_engine_versions": {"cwl-airflow": get_version()},
        }
        return response

    def wes_list_runs(self, page_size=None, page_token=None):
        logging.debug(
            f"Call wes_list_runs with page_size={page_size}, page_token={page_token}"
        )
        logging.debug(
            f"page_size and page_token are currently ignored by cwl-airflow api server"
        )
        dag_run_info = self.get_dag_runs()
        return [
            {
                "run_id": item["run_id"],
                "state": self.wes_state_conversion[item["state"]],
            }
            for item in dag_run_info["dag_runs"]
        ]

    def wes_run_workflow(self):
        logging.debug(f"Call wes_run_workflow")
        run_id = "".join(random.choice(string.ascii_lowercase) for i in range(32))
        try:
            tempdir, body = self.wes_collect_attachments(run_id)
            with open(path.join(DAGS_FOLDER, run_id + ".py"), "x") as o_stream:
                o_stream.write(
                    self.dag_template_with_tmp_folder.format(
                        body["workflow_url"], run_id, tempdir
                    )
                )
            self.post_dag_runs(
                dag_id=run_id,
                run_id=run_id,
                conf=json.dumps({"job": body["workflow_params"]}),
            )
            return {"run_id": run_id}
        except Exception as err:
            logging.debug(f"Failed to run workflow {err}")
            return connexion.problem(500, "Failed to run workflow", str(err))

    def wes_get_run_log(self, run_id):
        print(run_id)
        logging.debug(f"Call wes_get_run_log with {run_id}")
        try:
            dag_run_info = self.get_dag_runs(run_id=run_id)["dag_runs"][0]
            dag_run = DagRun.find(run_id=run_id, state=None)[0]
            workflow_params = dag_run.conf["job"]
            if "id" in workflow_params:
                del workflow_params["id"]
            workflow_outputs = {}
            try:
                results_location = dag_run.get_task_instance(
                    task_id="CWLJobGatherer"
                ).xcom_pull()
                with open(results_location, "r") as input_stream:
                    workflow_outputs = json.load(input_stream)
            except Exception as err:
                logging.debug(f"Failed to read workflow results from file. \n {err}")
            return {
                "run_id": run_id,
                "request": {"workflow_params": workflow_params},
                "state": self.wes_state_conversion[dag_run_info["state"]],
                "run_log": {
                    "name": run_id,
                    "cmd": [""],
                    "start_time": dag_run_info["start_date"],
                    "end_time": "",
                    "stdout": "",
                    "stderr": "",
                    "exit_code": "",
                },
                "task_logs": [{"name": task["id"]} for task in dag_run_info["tasks"]],
                "outputs": workflow_outputs,
            }
        except Exception as err:
            logging.debug(f"Failed to fetch infromation for {run_id}")
            return {}

    def wes_get_run_status(self, run_id):
        logging.debug(f"Call wes_get_run_status with run_id={run_id}")
        try:
            dag_run_info = self.get_dag_runs(dag_id=run_id, run_id=run_id)["dag_runs"][
                0
            ]
            return {
                "run_id": dag_run_info["run_id"],
                "state": self.wes_state_conversion[dag_run_info["state"]],
            }
        except Exception as err:
            logging.debug(f"Failed to fetch infromation for {run_id}")
            return {}

    def wes_cancel_run(self, run_id):
        logging.debug(f"Call wes_cancel_run with run_id={run_id}")
        try:
            dag_run = DagRun.find(dag_id=run_id, state=None)[0]
            self.stop_tasks(dag_run)
            self.remove_tmp_data(dag_run)
            return {"run_id": run_id}
        except Exception as err:
            logging.debug(f"Failed to cancel dag run {run_id}, {err}")
            return connexion.problem(
                500, f"Failed to cancel dag run {run_id}", str(err)
            )

    def stop_tasks(self, dr):
        logging.debug(f"Stop tasks for {dr.dag_id} - {dr.run_id}")
        for ti in dr.get_task_instances():
            logging.debug(
                f"process {ti.dag_id} - {ti.task_id} - {ti.execution_date} - {ti.pid}"
            )
            if ti.state == State.RUNNING:
                try:
                    process = psutil.Process(ti.pid) if ti.pid else None
                except Exception:
                    logging.debug(f" - cannot find process by PID {ti.pid}")
                    process = None
                ti.set_state(State.FAILED)
                logging.debug(" - set state to FAILED")
                if process:
                    logging.debug(f" - wait for process {ti.pid} to exit")
                    try:
                        cleanup_timeout = (
                            int(conf_get("core", "KILLED_TASK_CLEANUP_TIME", 60)) * 2
                        )
                        process.wait(
                            timeout=cleanup_timeout
                        )  # raises psutil.TimeoutExpired if timeout. Makes task fail -> DagRun fails
                    except psutil.TimeoutExpired as e:
                        logging.debug(f" - Done waiting for process {ti.pid} to die")

    def remove_tmp_data(self, dr):
        logging.debug(f"Remove tmp data for {dr.dag_id} - {dr.run_id}")
        tmp_folder = None
        try:
            results_location = dr.get_task_instance(
                task_id="CWLJobDispatcher"
            ).xcom_pull()
            with open(results_location, "r") as input_stream:
                dispatcher_outputs = json.load(input_stream)
                tmp_folder = dispatcher_outputs.get("tmp_folder")
        except Exception as err:
            logging.debug(f"Failed to read dispathcer results from file. \n {err}")
        try:
            shutil.rmtree(tmp_folder)
            logging.debug(f"Successfully removed {tmp_folder}")
        except Exception as ex:
            logging.error(
                f"Failed to delete temporary output directory {tmp_folder}\n {ex}"
            )
