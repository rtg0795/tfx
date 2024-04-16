# Copyright 2020 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Portable library for registering and publishing executions."""

from typing import Mapping, Optional, Sequence
import uuid

from tfx import types
from tfx.orchestration import data_types_utils
from tfx.orchestration import metadata
from tfx.orchestration.experimental.core import task as task_lib
from tfx.orchestration import datahub_utils
from tfx.orchestration.portable import merge_utils
from tfx.orchestration.portable.mlmd import execution_lib
from tfx.proto.orchestration import execution_result_pb2
from tfx.utils import typing_utils

from ml_metadata.proto import metadata_store_pb2


def publish_cached_executions(
    metadata_handle: metadata.Metadata,
    contexts: Sequence[metadata_store_pb2.Context],
    executions: Sequence[metadata_store_pb2.Execution],
    output_artifacts_maps: Optional[
        Sequence[typing_utils.ArtifactMultiMap]
    ] = None,
) -> None:
  """Marks an existing execution as using cached outputs from a previous execution.

  Args:
    metadata_handle: A handler to access MLMD.
    contexts: MLMD contexts to associated with the execution.
    executions: Executions that will be published as CACHED executions.
    output_artifacts_maps: A list of output artifacts of the executions. Each
      artifact will be linked with the execution through an event of type OUTPUT
  """
  for execution in executions:
    execution.last_known_state = metadata_store_pb2.Execution.CACHED

  execution_lib.put_executions(
      metadata_handle,
      executions,
      contexts,
      output_artifacts_maps=output_artifacts_maps,
  )


def set_execution_result_if_not_empty(
    executor_output: Optional[execution_result_pb2.ExecutorOutput],
    execution: metadata_store_pb2.Execution,
) -> None:
  """Sets execution result as a custom property of the execution."""
  if executor_output and (
      executor_output.execution_result.result_message
      or executor_output.execution_result.metadata_details
      or executor_output.execution_result.code
  ):
    execution_lib.set_execution_result(
        executor_output.execution_result, execution
    )


def publish_succeeded_execution(
    metadata_handle: metadata.Metadata,
    execution_id: int,
    contexts: Sequence[metadata_store_pb2.Context],
    output_artifacts: Optional[typing_utils.ArtifactMultiMap] = None,
    executor_output: Optional[execution_result_pb2.ExecutorOutput] = None,
    task: Optional[task_lib.ExecNodeTask] = None,
) -> tuple[
    Optional[typing_utils.ArtifactMultiMap],
    metadata_store_pb2.Execution,
]:
  """Marks an existing execution as success.

  Also publishes the output artifacts produced by the execution. This method
  will also merge the executor produced info into system generated output
  artifacts. The `last_know_state` of the execution will be changed to
  `COMPLETE` and the output artifacts will be marked as `LIVE`.

  Args:
    metadata_handle: A handler to access MLMD.
    execution_id: The id of the execution to mark successful.
    contexts: MLMD contexts to associated with the execution.
    output_artifacts: Output artifacts skeleton of the execution, generated by
      the system. Each artifact will be linked with the execution through an
      event with type OUTPUT.
    executor_output: Executor outputs. `executor_output.output_artifacts` will
      be used to update system-generated output artifacts passed in through
      `output_artifacts` arg. There are three constraints to the update: 1. The
      keys in `executor_output.output_artifacts` are expected to be a subset of
      the system-generated output artifacts dict. 2. An update to a certain key
      should contains all the artifacts under that key. 3. An update to an
      artifact should not change the type of the artifact.
    task: the task that just completed its component execution.

  Returns:
    The tuple containing the maybe updated output_artifacts (note that only
    outputs whose key are in executor_output will be updated and others will be
    untouched, that said, it can be partially updated) and the written
    execution.
  Raises:
    RuntimeError: if the executor output to a output channel is partial.
  """
  unpacked_output_artifacts = (
      None  # pylint: disable=g-long-ternary
      if executor_output is None
      else (
          data_types_utils.unpack_executor_output_artifacts(
              executor_output.output_artifacts
          )
      )
  )
  # TODO(b/300541907) Address corner case if the node returns an ExecutorOutput
  # that contains new or updated artifacts for the intermediate output key,
  # which is not supported.
  merged_output_artifacts = merge_utils.merge_updated_output_artifacts(
      output_artifacts, unpacked_output_artifacts
  )

  output_artifacts_to_publish = {}
  for key, artifacts in merged_output_artifacts.items():
    output_artifacts_to_publish[key] = []
    for artifact in artifacts:
      if artifact.state != types.artifact.ArtifactState.REFERENCE:
        # Mark output artifact as PUBLISHED (LIVE in MLMD) if it was not in
        # state REFERENCE.
        artifact.state = types.artifact.ArtifactState.PUBLISHED

        # TODO(b/300541196): Investigate if/how this affects governance.
        # We don't want to create an OUTPUT_EVENT for the REFERENCE artifact
        # used for intermediate artifact emission. However, a
        # PENDING_OUTPUT_EVENT created by the governance task scheduler will
        # remain in MLMD.
        output_artifacts_to_publish[key].append(artifact)

  [execution] = metadata_handle.store.get_executions_by_id([execution_id])
  execution.last_known_state = metadata_store_pb2.Execution.COMPLETE
  if executor_output:
    for key, value in executor_output.execution_properties.items():
      execution.custom_properties[key].CopyFrom(value)
  set_execution_result_if_not_empty(executor_output, execution)

  execution = execution_lib.put_execution(
      metadata_handle,
      execution,
      contexts,
      output_artifacts=output_artifacts_to_publish,
  )

  datahub_utils.log_component_execution(
      execution, task, output_artifacts_to_publish
  )

  return output_artifacts_to_publish, execution


def publish_failed_execution(
    metadata_handle: metadata.Metadata,
    contexts: Sequence[metadata_store_pb2.Context],
    execution_id: int,
    executor_output: Optional[execution_result_pb2.ExecutorOutput] = None,
) -> None:
  """Marks an existing execution as failed.

  Args:
    metadata_handle: A handler to access MLMD.
    contexts: MLMD contexts to associated with the execution.
    execution_id: The id of the execution.
    executor_output: The output of executor.
  """
  [execution] = metadata_handle.store.get_executions_by_id([execution_id])
  execution.last_known_state = metadata_store_pb2.Execution.FAILED
  set_execution_result_if_not_empty(executor_output, execution)

  execution_lib.put_execution(metadata_handle, execution, contexts)


def publish_internal_execution(
    metadata_handle: metadata.Metadata,
    contexts: Sequence[metadata_store_pb2.Context],
    execution_id: int,
    output_artifacts: Optional[typing_utils.ArtifactMultiMap] = None,
) -> None:
  """Marks an exeisting execution as as success and links its output to an INTERNAL_OUTPUT event.

  Args:
    metadata_handle: A handler to access MLMD.
    contexts: MLMD contexts to associated with the execution.
    execution_id: The id of the execution.
    output_artifacts: Output artifacts of the execution. Each artifact will be
      linked with the execution through an event with type INTERNAL_OUTPUT.
  """
  [execution] = metadata_handle.store.get_executions_by_id([execution_id])
  execution.last_known_state = metadata_store_pb2.Execution.COMPLETE

  execution_lib.put_execution(
      metadata_handle,
      execution,
      contexts,
      output_artifacts=output_artifacts,
      output_event_type=metadata_store_pb2.Event.INTERNAL_OUTPUT,
  )


def register_execution(
    metadata_handle: metadata.Metadata,
    execution_type: metadata_store_pb2.ExecutionType,
    contexts: Sequence[metadata_store_pb2.Context],
    input_artifacts: Optional[typing_utils.ArtifactMultiMap] = None,
    exec_properties: Optional[Mapping[str, types.ExecPropertyTypes]] = None,
    last_known_state: metadata_store_pb2.Execution.State = metadata_store_pb2.Execution.RUNNING,
) -> metadata_store_pb2.Execution:
  """Registers a new execution in MLMD.

  Along with the execution:
  -  the input artifacts will be linked to the execution.
  -  the contexts will be linked to both the execution and its input artifacts.

  Args:
    metadata_handle: A handler to access MLMD.
    execution_type: The type of the execution.
    contexts: MLMD contexts to associated with the execution.
    input_artifacts: Input artifacts of the execution. Each artifact will be
      linked with the execution through an event.
    exec_properties: Execution properties. Will be attached to the execution.
    last_known_state: The last known state of the execution.

  Returns:
    An MLMD execution that is registered in MLMD, with id populated.
  """
  # Setting exec_name is required to make sure that only one execution is
  # registered in MLMD. If there is a RPC retry, AlreadyExistError will raise.
  # After this fix (b/221103319), AlreadyExistError may not raise. Instead,
  # execution may be updated again upon RPC retries.
  exec_name = str(uuid.uuid4())
  execution = execution_lib.prepare_execution(
      metadata_handle,
      execution_type,
      last_known_state,
      exec_properties,
      execution_name=exec_name,
  )

  return execution_lib.put_execution(
      metadata_handle, execution, contexts, input_artifacts=input_artifacts
  )
