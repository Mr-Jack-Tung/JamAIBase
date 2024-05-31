import asyncio
import pathlib
from datetime import timedelta
from hashlib import blake2b
from typing import Annotated, Any

import numpy as np
from fastapi import (
    APIRouter,
    BackgroundTasks,
    File,
    Form,
    Header,
    Path,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from filelock import FileLock, Timeout
from loguru import logger
from pydantic_settings import BaseSettings, SettingsConfigDict

from jamaibase import protocol as p
from owl.db.file import FileTable
from owl.db.gen_executor import MultiRowsGenExecutor
from owl.db.gen_table import ActionTable, ChatTable, GenerativeTable, KnowledgeTable
from owl.llm import model_names, predict
from owl.loaders import load_file, split_chunks
from owl.models import CloudEmbedder
from owl.utils import get_api_key
from owl.utils.exceptions import OwlException, TableSchemaFixedError
from owl.utils.tasks import repeat_every


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    owl_db_dir: str = "db"
    owl_reindex_period_sec: int = 60
    owl_immediate_reindex_max_rows: int = 2000
    owl_optimize_period_sec: int = 60
    owl_remove_version_older_than_days: int = 7
    owl_concurrent_rows_batch_size: int = 3
    owl_concurrent_cols_batch_size: int = 5


config = Config()
router = APIRouter()


@router.on_event("startup")
async def startup():
    # Router lifespan is broken as of fastapi==0.109.0 and starlette==0.35.1
    # https://github.com/tiangolo/fastapi/discussions/9664
    logger.info(f"GenTable router config: {config}")


def _get_gen_table(
    org_id: str,
    project_id: str,
    table_type: p.TableType,
) -> GenerativeTable:
    lance_path = f"{config.owl_db_dir}/{org_id}/{project_id}/{table_type.value}"
    sqlite_path = f"sqlite:///{lance_path}.db"
    read_consistency_interval = timedelta(seconds=0)
    if table_type == table_type.action:
        return ActionTable(
            sqlite_path, lance_path, read_consistency_interval=read_consistency_interval
        )
    elif table_type == table_type.knowledge:
        return KnowledgeTable(
            sqlite_path, lance_path, read_consistency_interval=read_consistency_interval
        )
    else:
        return ChatTable(
            sqlite_path, lance_path, read_consistency_interval=read_consistency_interval
        )


def _get_file_table(
    org_id: str,
    project_id: str,
) -> FileTable:
    return FileTable(
        f"{config.owl_db_dir}/{org_id}/{project_id}/file",
        table_name="file",
        read_consistency_interval=timedelta(seconds=0),
    )


def _iter_all_tables(batch_size: int = 200):
    table_types = [p.TableType.action, p.TableType.knowledge, p.TableType.chat]
    db_dir = pathlib.Path(config.owl_db_dir)
    for org_dir in db_dir.iterdir():
        if not org_dir.is_dir():
            continue
        for project_dir in org_dir.iterdir():
            if not project_dir.is_dir():
                continue
            for table_type in table_types:
                table = _get_gen_table(org_dir.name, project_dir.name, table_type)
                with table.create_session() as session:
                    offset, total = 0, 1
                    while offset < total:
                        metas, total = table.list_meta(
                            session,
                            offset=offset,
                            limit=batch_size,
                            remove_state_cols=True,
                            parent_id=None,
                        )
                        offset += batch_size
                        for meta in metas:
                            yield session, table, meta, f"{project_dir}/{table_type.value}/{meta.id}"
            table = _get_file_table(org_dir.name, project_dir.name)
            yield None, table, None, f"{project_dir}/file/file"


@router.on_event("startup")
@repeat_every(seconds=config.owl_reindex_period_sec, wait_first=True)
async def periodic_reindex():
    lock = FileLock(f"{config.owl_db_dir}/periodic_reindex.lock", blocking=False)
    try:
        with lock:
            logger.info("Periodic Lance re-indexing started.")
            num_ok = num_skipped = num_failed = 0
            for session, table, meta, table_path in _iter_all_tables():
                if session is None:
                    continue
                try:
                    reindexed = table.create_indexes(session, meta.id)
                    if reindexed:
                        num_ok += 1
                    else:
                        num_skipped += 1
                except Exception:
                    logger.exception(f"Periodic Lance re-indexing failed for table: {table_path}")
                    num_failed += 1
        logger.info(
            (
                "Periodic Lance re-indexing completed "
                f"({num_ok:,d} OK, {num_skipped:,d} skipped, {num_failed:,d} failed)."
            )
        )
    except Timeout:
        logger.info("Periodic Lance re-indexing skipped.")
    except Exception:
        logger.exception("Periodic Lance re-indexing encountered an error.")


@router.on_event("startup")
@repeat_every(seconds=config.owl_optimize_period_sec, wait_first=True)
async def periodic_optimize():
    lock = FileLock(f"{config.owl_db_dir}/periodic_optimization.lock", blocking=False)
    try:
        with lock:
            logger.info("Periodic Lance optimization started.")
            num_ok = num_skipped = num_failed = 0
            for _, table, meta, table_path in _iter_all_tables():
                done = True
                try:
                    if meta is None:
                        done = done and table.compact_files()
                        done = done and table.cleanup_old_versions(
                            older_than=timedelta(days=config.owl_remove_version_older_than_days),
                        )
                    else:
                        done = done and table.compact_files(meta.id)
                        done = done and table.cleanup_old_versions(
                            meta.id,
                            older_than=timedelta(days=config.owl_remove_version_older_than_days),
                        )
                    if done:
                        num_ok += 1
                    else:
                        num_skipped += 1
                except Exception:
                    logger.exception(f"Periodic Lance optimization failed for table: {table_path}")
                    num_failed += 1
        logger.info(
            (
                "Periodic Lance optimization completed "
                f"({num_ok:,d} OK, {num_skipped:,d} skipped, {num_failed:,d} failed)."
            )
        )
    except Timeout:
        logger.info("Periodic Lance optimization skipped.")
    except Exception:
        logger.exception("Periodic Lance optimization encountered an error.")


def _create_table(
    request: Request,
    table_type: p.TableType,
    schema: p.TableSchemaCreate,
) -> p.TableMetaResponse:
    try:
        table = _get_gen_table(request.state.org_id, request.state.project_id, table_type)
        with table.create_session() as session:
            _, meta = table.create_table(session, schema)
            meta = p.TableMetaResponse.model_validate(
                meta, update={"num_rows": table.count_rows(schema.id)}
            )
            return meta
    except OwlException:
        raise
    except Exception:
        logger.exception(
            f"[{request.state.org_id}/{request.state.project_id}] Failed to create table: {schema}"
        )
        raise


@router.post("/v1/gen_tables/action")
def create_action_table(
    request: Request, schema: p.ActionTableSchemaCreate
) -> p.TableMetaResponse:
    return _create_table(request, p.TableType.action, schema)


@router.post("/v1/gen_tables/knowledge")
def create_knowledge_table(
    request: Request, schema: p.KnowledgeTableSchemaCreate
) -> p.TableMetaResponse:
    return _create_table(request, p.TableType.knowledge, schema)


@router.post("/v1/gen_tables/chat")
def create_chat_table(request: Request, schema: p.ChatTableSchemaCreate) -> p.TableMetaResponse:
    return _create_table(request, p.TableType.chat, schema)


@router.post("/v1/gen_tables/{table_type}/duplicate/{table_id_src}/{table_id_dst}")
def duplicate_table(
    *,
    request: Request,
    table_type: p.TableType,
    table_id_src: str = Path(pattern=p.TABLE_NAME_PATTERN, description="Source table name or ID."),
    table_id_dst: str = Path(
        pattern=p.TABLE_NAME_PATTERN, description="Destination table name or ID."
    ),
    include_data: bool = Query(
        default=True,
        description="_Optional_. Whether to include the data from the source table in the duplicated table. Defaults to `True`.",
    ),
    deploy: bool = Query(
        default=False,
        description="_Optional_. Whether to deploy the duplicated table. Defaults to `False`.",
    ),
) -> p.TableMetaResponse:
    try:
        table = _get_gen_table(request.state.org_id, request.state.project_id, table_type)
        with table.create_session() as session:
            meta = table.duplicate_table(session, table_id_src, table_id_dst, include_data, deploy)
            meta = p.TableMetaResponse.model_validate(
                meta, update={"num_rows": table.count_rows(table_id_dst)}
            )
            return meta
    except OwlException:
        raise
    except Exception:
        logger.exception(
            (
                f"Failed to duplicate table: {table_id_src} -> {table_id_dst} "
                f"include_data={include_data}"
            )
        )
        raise


@router.post("/v1/gen_tables/{table_type}/rename/{table_id_src}/{table_id_dst}")
def rename_table(
    request: Request,
    table_type: p.TableType,
    table_id_src: Annotated[str, Path(description="Source table name or ID.")],  # Don't validate
    table_id_dst: Annotated[
        str,
        Path(
            pattern=p.TABLE_NAME_PATTERN,
            description="Destination table name or ID.",
        ),
    ],
) -> p.TableMetaResponse:
    try:
        table = _get_gen_table(request.state.org_id, request.state.project_id, table_type)
        with table.create_session() as session:
            meta = table.rename_table(session, table_id_src, table_id_dst)
            meta = p.TableMetaResponse.model_validate(
                meta, update={"num_rows": table.count_rows(table_id_dst)}
            )
            return meta
    except OwlException:
        raise
    except Exception:
        logger.exception(f"Failed to rename table: {table_id_src} -> {table_id_dst}")
        raise


@router.delete("/v1/gen_tables/{table_type}/{table_id}")
def delete_table(
    request: Request,
    table_type: p.TableType,
    table_id: Annotated[str, Path(description="The ID of the table to delete.")],  # Don't validate
) -> p.OkResponse:
    try:
        table = _get_gen_table(request.state.org_id, request.state.project_id, table_type)
        with table.create_session() as session:
            table.delete_table(session, table_id)
            return p.OkResponse()
    except OwlException:
        raise
    except Exception:
        logger.exception(f"Failed to create table: {table_id}")
        raise


@router.get("/v1/gen_tables/{table_type}")
def list_tables(
    request: Request,
    table_type: p.TableType,
    offset: int = Query(
        default=0,
        ge=0,
        description="_Optional_. Pagination offset. Defaults to 0.",
    ),
    limit: int = Query(
        default=100,
        gt=0,
        le=100,
        description="_Optional_. Number of tables to return (min 1, max 100). Defaults to 100.",
    ),
    parent_id: Annotated[
        str | None,
        Query(
            description=(
                "_Optional_. Parent ID of tables to return. "
                "Defaults to None (return all action and knowledge tables, return all Chat Agent)."
            ),
        ),
    ] = None,
) -> p.Page[p.TableMetaResponse]:
    logger.info(
        (
            f"{request.state.id} - Listing gen tables "
            f"(table_type={table_type}  offset={offset}  limit={limit}  parent_id={parent_id})"
        )
    )
    try:
        table = _get_gen_table(request.state.org_id, request.state.project_id, table_type)
        with table.create_session() as session:
            metas, total = table.list_meta(
                session,
                offset=offset,
                limit=limit,
                remove_state_cols=True,
                parent_id=parent_id,
            )
            return p.Page[p.TableMetaResponse](
                items=metas,
                offset=offset,
                limit=limit,
                total=total,
            )
    except OwlException:
        raise
    except Exception:
        logger.exception(f"Failed to list tables")
        raise


@router.get("/v1/gen_tables/{table_type}/{table_id}")
def get_table(
    request: Request,
    table_type: p.TableType,
    table_id: str = Path(
        pattern=p.TABLE_NAME_PATTERN, description="The ID of the table to fetch."
    ),
) -> p.TableMetaResponse:
    try:
        table = _get_gen_table(request.state.org_id, request.state.project_id, table_type)
        with table.create_session() as session:
            meta = table.open_meta(session, table_id, remove_state_cols=True)
            meta = p.TableMetaResponse.model_validate(
                meta, update={"num_rows": table.count_rows(table_id)}
            )
            return meta
    except OwlException:
        raise
    except Exception:
        logger.exception(f"Failed to get table: {table_id}")
        raise


@router.post("/v1/gen_tables/{table_type}/gen_config/update")
def update_gen_config(
    request: Request,
    table_type: p.TableType,
    updates: p.GenConfigUpdateRequest,
) -> p.TableMetaResponse:
    try:
        table = _get_gen_table(request.state.org_id, request.state.project_id, table_type)
        with table.create_session() as session:
            meta = table.update_gen_config(session, updates)
            meta = p.TableMetaResponse.model_validate(
                meta, update={"num_rows": table.count_rows(updates.table_id)}
            )
            return meta
    except OwlException:
        raise
    except Exception:
        logger.exception(f"Failed to update generation config: {updates}")
        raise


def _add_columns(
    request: Request,
    table_type: p.TableType,
    schema: p.TableSchemaCreate,
) -> p.TableMetaResponse:
    try:
        table = _get_gen_table(request.state.org_id, request.state.project_id, table_type)
        with table.create_session() as session:
            _, meta = table.add_columns(session, schema)
            meta = p.TableMetaResponse.model_validate(
                meta, update={"num_rows": table.count_rows(schema.id)}
            )
            return meta
    except OwlException:
        raise
    except Exception:
        logger.exception(f"Failed to add columns to table: {schema}")
        raise


@router.post("/v1/gen_tables/action/columns/add")
def add_action_columns(request: Request, schema: p.AddActionColumnSchema) -> p.TableMetaResponse:
    return _add_columns(request, p.TableType.action, schema)


@router.post("/v1/gen_tables/knowledge/columns/add")
def add_knowledge_columns(
    request: Request, schema: p.AddKnowledgeColumnSchema
) -> p.TableMetaResponse:
    return _add_columns(request, p.TableType.knowledge, schema)


@router.post("/v1/gen_tables/chat/columns/add")
def add_chat_columns(request: Request, schema: p.AddChatColumnSchema) -> p.TableMetaResponse:
    return _add_columns(request, p.TableType.chat, schema)


@router.post("/v1/gen_tables/{table_type}/columns/drop")
def drop_columns(
    request: Request,
    bg_tasks: BackgroundTasks,
    table_type: p.TableType,
    body: p.ColumnDropRequest,
) -> p.TableMetaResponse:
    try:
        table = _get_gen_table(request.state.org_id, request.state.project_id, table_type)
        with table.create_session() as session:
            _, meta = table.drop_columns(session, body.table_id, body.column_names)
            meta = p.TableMetaResponse.model_validate(
                meta, update={"num_rows": table.count_rows(body.table_id)}
            )
            bg_tasks.add_task(table.create_indexes, session, body.table_id)
            return meta
    except OwlException:
        raise
    except Exception:
        logger.exception(f"Failed to drop columns from table: {body}")
        raise


@router.post("/v1/gen_tables/{table_type}/columns/rename")
def rename_columns(
    request: Request,
    table_type: p.TableType,
    body: p.ColumnRenameRequest,
) -> p.TableMetaResponse:
    try:
        table = _get_gen_table(request.state.org_id, request.state.project_id, table_type)
        with table.create_session() as session:
            meta = table.rename_columns(session, body.table_id, body.column_map)
            meta = p.TableMetaResponse.model_validate(
                meta, update={"num_rows": table.count_rows(body.table_id)}
            )
            return meta
    except OwlException:
        raise
    except Exception:
        logger.exception(f"Failed to rename columns of table: {body}")
        raise


@router.post("/v1/gen_tables/{table_type}/columns/reorder")
def reorder_columns(
    request: Request,
    table_type: p.TableType,
    body: p.ColumnReorderRequest,
) -> p.TableMetaResponse:
    try:
        table = _get_gen_table(request.state.org_id, request.state.project_id, table_type)
        with table.create_session() as session:
            meta = table.reorder_columns(session, body.table_id, body.column_names)
            meta = p.TableMetaResponse.model_validate(
                meta, update={"num_rows": table.count_rows(body.table_id)}
            )
        return meta
    except OwlException:
        raise
    except Exception:
        logger.exception(f"Failed to reorder columns of table: {body}")
        raise


@router.get("/v1/gen_tables/{table_type}/{table_id}/rows")
def list_rows(
    *,
    request: Request,
    table_type: p.TableType,
    table_id: str = Path(pattern=p.TABLE_NAME_PATTERN, description="Table ID or name."),
    offset: int = Query(
        default=0,
        ge=0,
        description="_Optional_. Pagination offset. Defaults to 0.",
    ),
    limit: int = Query(
        default=100,
        gt=0,
        le=100,
        description="_Optional_. Number of rows to return (min 1, max 100). Defaults to 100.",
    ),
    columns: list[p.Name] | None = Query(
        default=None,
        description="_Optional_. A list of column names to include in the response. If not provided, all columns will be returned.",
    ),
) -> p.Page[dict[p.Name, Any]]:
    try:
        table = _get_gen_table(request.state.org_id, request.state.project_id, table_type)
        rows, total = table.list_rows(
            table_id=table_id,
            offset=offset,
            limit=limit,
            columns=columns,
            convert_null=True,
            remove_state_cols=True,
            json_safe=True,
            include_original=True,
        )
        return p.Page[dict[p.Name, Any]](items=rows, offset=offset, limit=limit, total=total)
    except OwlException:
        raise
    except Exception:
        logger.exception(f"Failed to get rows from table: {table_id}")
        raise


@router.get("/v1/gen_tables/{table_type}/{table_id}/rows/{row_id}")
def get_row(
    *,
    request: Request,
    table_type: p.TableType,
    table_id: str = Path(pattern=p.TABLE_NAME_PATTERN, description="Table ID or name."),
    row_id: Annotated[str, Path(description="The ID of the specific row to fetch.")],
    columns: list[p.Name] | None = Query(
        default=None,
        description="_Optional_. A list of column names to include in the response. If not provided, all columns will be returned.",
    ),
) -> dict[p.Name, Any]:
    try:
        table = _get_gen_table(request.state.org_id, request.state.project_id, table_type)
        row = table.get_row(
            table_id,
            row_id,
            columns=columns,
            convert_null=True,
            remove_state_cols=True,
            json_safe=True,
            include_original=True,
        )
        return row
    except OwlException:
        raise
    except Exception:
        logger.exception(f"Failed to get row '{row_id}' from table: {table_id}")
        raise


@router.post("/v1/gen_tables/{table_type}/rows/add")
async def add_rows(
    request: Request,
    bg_tasks: BackgroundTasks,
    table_type: p.TableType,
    body: p.RowAddRequest,
    openai_api_key: Annotated[str, Header(description="OpenAI API key.")] = "",
    anthropic_api_key: Annotated[str, Header(description="Anthropic API key.")] = "",
    gemini_api_key: Annotated[str, Header(description="Google Gemini API key.")] = "",
    cohere_api_key: Annotated[str, Header(description="Cohere API key.")] = "",
    groq_api_key: Annotated[str, Header(description="Groq API key.")] = "",
    together_api_key: Annotated[str, Header(description="Together AI API key.")] = "",
    jina_api_key: Annotated[str, Header(description="Jina API key.")] = "",
    voyage_api_key: Annotated[str, Header(description="Voyage API key.")] = "",
):
    try:
        table = _get_gen_table(request.state.org_id, request.state.project_id, table_type)
        if body.reindex or (
            body.reindex is None
            and table.count_rows(body.table_id) <= config.owl_immediate_reindex_max_rows
        ):
            with table.create_session() as session:
                bg_tasks.add_task(
                    table.create_indexes,
                    session,
                    body.table_id,
                )

        executor = MultiRowsGenExecutor(
            table,
            request=request,
            body=body,
            rows_batch_size=config.owl_concurrent_rows_batch_size,
            cols_batch_size=config.owl_concurrent_cols_batch_size,
            openai_api_key=openai_api_key,
            anthropic_api_key=anthropic_api_key,
            gemini_api_key=gemini_api_key,
            cohere_api_key=cohere_api_key,
            groq_api_key=groq_api_key,
            together_api_key=together_api_key,
            jina_api_key=jina_api_key,
            voyage_api_key=voyage_api_key,
        )
        if body.stream:
            return StreamingResponse(
                content=await executor.gen_rows(),
                status_code=200,
                media_type="text/event-stream",
                headers={"X-Accel-Buffering": "no"},
            )
        else:
            return await executor.gen_rows()
    except OwlException:
        raise
    except Exception:
        logger.exception(f"Failed to add row to table: {body}")
        raise


@router.post("/v1/gen_tables/{table_type}/rows/regen")
async def regen_rows(
    request: Request,
    bg_tasks: BackgroundTasks,
    table_type: p.TableType,
    body: p.RowRegenRequest,
    openai_api_key: Annotated[str, Header(description="OpenAI API key.")] = "",
    anthropic_api_key: Annotated[str, Header(description="Anthropic API key.")] = "",
    gemini_api_key: Annotated[str, Header(description="Google Gemini API key.")] = "",
    cohere_api_key: Annotated[str, Header(description="Cohere API key.")] = "",
    groq_api_key: Annotated[str, Header(description="Groq API key.")] = "",
    together_api_key: Annotated[str, Header(description="Together AI API key.")] = "",
    jina_api_key: Annotated[str, Header(description="Jina API key.")] = "",
    voyage_api_key: Annotated[str, Header(description="Voyage API key.")] = "",
):
    try:
        table = _get_gen_table(request.state.org_id, request.state.project_id, table_type)
        if body.reindex or (
            body.reindex is None
            and table.count_rows(body.table_id) <= config.owl_immediate_reindex_max_rows
        ):
            with table.create_session() as session:
                bg_tasks.add_task(
                    table.create_indexes,
                    session,
                    body.table_id,
                )

        executor = MultiRowsGenExecutor(
            table,
            request=request,
            body=body,
            rows_batch_size=config.owl_concurrent_rows_batch_size,
            cols_batch_size=config.owl_concurrent_cols_batch_size,
            openai_api_key=openai_api_key,
            anthropic_api_key=anthropic_api_key,
            gemini_api_key=gemini_api_key,
            cohere_api_key=cohere_api_key,
            groq_api_key=groq_api_key,
            together_api_key=together_api_key,
            jina_api_key=jina_api_key,
            voyage_api_key=voyage_api_key,
        )
        if body.stream:
            return StreamingResponse(
                content=await executor.gen_rows(),
                status_code=200,
                media_type="text/event-stream",
                headers={"X-Accel-Buffering": "no"},
            )
        else:
            return await executor.gen_rows()
    except OwlException:
        raise
    except Exception:
        logger.exception(f"Failed to regen row of table: {body}")
        raise


@router.post("/v1/gen_tables/{table_type}/rows/update")
def update_row(
    request: Request,
    bg_tasks: BackgroundTasks,
    table_type: p.TableType,
    body: p.RowUpdateRequest,
) -> p.OkResponse:
    try:
        table = _get_gen_table(request.state.org_id, request.state.project_id, table_type)
        if table_type == p.TableType.knowledge:
            col_names = set(n.lower() for n in body.data.keys())
            if "text embed" in col_names or "title embed" in col_names:
                raise TableSchemaFixedError("Cannot update 'Text Embed' or 'Title Embed'.")
        with table.create_session() as session:
            table.update_rows(
                session,
                body.table_id,
                f"`ID` = '{body.row_id}'",
                values=body.data,
            )
            if body.reindex or (
                body.reindex is None
                and table.count_rows(body.table_id) <= config.owl_immediate_reindex_max_rows
            ):
                bg_tasks.add_task(table.create_indexes, session, body.table_id)
        return p.OkResponse()
    except OwlException:
        raise
    except Exception:
        logger.exception(f"Failed to update rows (by ID) of table: {body}")
        raise


@router.post("/v1/gen_tables/{table_type}/rows/delete")
def delete_rows(
    request: Request,
    bg_tasks: BackgroundTasks,
    table_type: p.TableType,
    body: p.RowDeleteRequest,
) -> p.OkResponse:
    try:
        table = _get_gen_table(request.state.org_id, request.state.project_id, table_type)
        with table.create_session() as session:
            table.delete_rows(session, body.table_id, body.where)
            if body.reindex or (
                body.reindex is None
                and table.count_rows(body.table_id) <= config.owl_immediate_reindex_max_rows
            ):
                bg_tasks.add_task(table.create_indexes, session, body.table_id)
        return p.OkResponse()
    except OwlException:
        raise
    except Exception:
        logger.exception(f"Failed to delete rows from table: {body}")
        raise


@router.delete("/v1/gen_tables/{table_type}/{table_id}/rows/{row_id}")
def delete_row(
    request: Request,
    bg_tasks: BackgroundTasks,
    table_type: p.TableType,
    table_id: str = Path(pattern=p.TABLE_NAME_PATTERN, description="Table ID or name."),
    row_id: str = Path(description="The ID of the specific row to delete."),
    reindex: Annotated[bool, Query(description="Whether to reindex immediately.")] = True,
) -> p.OkResponse:
    try:
        table = _get_gen_table(request.state.org_id, request.state.project_id, table_type)
        with table.create_session() as session:
            table.delete_row(session, table_id, row_id)
            if reindex:
                bg_tasks.add_task(table.create_indexes, session, table_id)
        return p.OkResponse()
    except OwlException:
        raise
    except Exception:
        logger.exception(f"Failed to delete row '{row_id}' from table: {table_id}")
        raise


@router.post("/v1/gen_tables/{table_type}/hybrid_search")
def hybrid_search(
    request: Request,
    table_type: p.TableType,
    body: p.SearchRequest,
    openai_api_key: Annotated[str, Header(description="OpenAI API key.")] = "",
    anthropic_api_key: Annotated[str, Header(description="Anthropic API key.")] = "",
    gemini_api_key: Annotated[str, Header(description="Google Gemini API key.")] = "",
    cohere_api_key: Annotated[str, Header(description="Cohere API key.")] = "",
    groq_api_key: Annotated[str, Header(description="Groq API key.")] = "",
    together_api_key: Annotated[str, Header(description="Together AI API key.")] = "",
    jina_api_key: Annotated[str, Header(description="Jina API key.")] = "",
    voyage_api_key: Annotated[str, Header(description="Voyage API key.")] = "",
) -> list[dict[p.Name, Any]]:
    try:
        table = _get_gen_table(request.state.org_id, request.state.project_id, table_type)
        with table.create_session() as session:
            rows = table.hybrid_search(
                session,
                body.table_id,
                query=body.query,
                where=body.where,
                limit=body.limit,
                metric=body.metric,
                nprobes=body.nprobes,
                refine_factor=body.refine_factor,
                reranking_model=body.reranking_model,
                convert_null=True,
                remove_state_cols=True,
                json_safe=True,
                include_original=True,
                openai_api_key=openai_api_key,
                anthropic_api_key=anthropic_api_key,
                gemini_api_key=gemini_api_key,
                cohere_api_key=cohere_api_key,
                groq_api_key=groq_api_key,
                together_api_key=together_api_key,
                jina_api_key=jina_api_key,
                voyage_api_key=voyage_api_key,
            )
        return rows
    except OwlException:
        raise
    except Exception:
        logger.exception(f"Failed to search table: {body}")
        raise


def list_files():
    pass


def _embed(embedder: CloudEmbedder, texts: list[str], embed_dtype: str) -> np.ndarray:
    embeddings = embedder.embed_documents(texts=texts)
    embeddings = np.asarray(embeddings, dtype=embed_dtype)
    embeddings = embeddings / np.linalg.norm(embeddings, axis=1, keepdims=True)
    return embeddings


async def _add_file(
    request: Request,
    bg_tasks: BackgroundTasks,
    request_id: str,
    table_id: str,
    file_info: dict,
    chunk_size: int,
    chunk_overlap: int,
    openai_api_key: str = "",
    anthropic_api_key: str = "",
    gemini_api_key: str = "",
    cohere_api_key: str = "",
    groq_api_key: str = "",
    together_api_key: str = "",
    jina_api_key: str = "",
    voyage_api_key: str = "",
) -> p.OkResponse:
    file_name = file_info["File Name"]
    chunks = load_file(file_name, file_info["Content"])
    logger.debug("Splitting file: {file_name}", file_name=file_name)
    chunks = split_chunks(
        p.SplitChunksRequest(
            chunks=chunks,
            params=p.SplitChunksParams(
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            ),
        )
    )

    # --- Extract title --- #
    excerpt = "".join(d.text for d in chunks[:8])
    model = await model_names(
        prefer=p.DEFAULT_CHAT_MODEL,
        capabilities=["chat"],
        openai_api_key=openai_api_key,
        anthropic_api_key=anthropic_api_key,
        gemini_api_key=gemini_api_key,
        cohere_api_key=cohere_api_key,
        groq_api_key=groq_api_key,
        together_api_key=together_api_key,
        jina_api_key=jina_api_key,
        voyage_api_key=voyage_api_key,
    )
    model = model[0]
    logger.debug(f"{request_id} - Performing title extraction using: {model}")
    try:
        response: p.ChatCompletionChunk = await predict(
            request=request,
            model=model,
            messages=[
                p.ChatEntry.system("You are an concise assistant."),
                p.ChatEntry.user(
                    (
                        f"CONTEXT:\n{excerpt}\n\n"
                        "From the excerpt, extract the document title or guess a possible title. "
                        "Provide the title without explanation."
                    )
                ),
            ],
            max_tokens=200,
            temperature=0.01,
            top_p=0.01,
            stream=False,
            openai_api_key=openai_api_key,
            anthropic_api_key=anthropic_api_key,
            gemini_api_key=gemini_api_key,
            cohere_api_key=cohere_api_key,
            groq_api_key=groq_api_key,
            together_api_key=together_api_key,
            jina_api_key=jina_api_key,
            voyage_api_key=voyage_api_key,
        )
        title = response.text.strip()
        if title.startswith('"') and title.endswith('"'):
            title = title[1:-1]
    except Exception:
        logger.exception(f"{request_id} - Title extraction errored for excerpt: \n{excerpt}\n")
        title = ""

    # --- Add into Knowledge Table --- #
    table = _get_gen_table(request.state.org_id, request.state.project_id, p.TableType.knowledge)
    with table.create_session() as session:
        meta = table.open_meta(session, table_id)
        title_embed = None
        text_embeds = []
        for col in meta.cols:
            if col["vlen"] == 0:
                continue
            gen_config = p.EmbedGenConfig.model_validate(col["gen_config"])
            embedding_model = gen_config.embedding_model
            api_key = get_api_key(
                embedding_model,
                openai_api_key=openai_api_key,
                anthropic_api_key=anthropic_api_key,
                gemini_api_key=gemini_api_key,
                cohere_api_key=cohere_api_key,
                groq_api_key=groq_api_key,
                together_api_key=together_api_key,
                jina_api_key=jina_api_key,
                voyage_api_key=voyage_api_key,
            )
            embedder = CloudEmbedder(
                embedder_name=embedding_model,
                api_key=api_key,
            )
            if col["id"] == "Title Embed":
                title_embed = _embed(embedder, [title], col["dtype"])[0]
            elif col["id"] == "Text Embed":
                text_embeds = _embed(embedder, [chunk.text for chunk in chunks], col["dtype"])
            else:
                continue
        if title_embed is None or len(text_embeds) == 0:
            raise RuntimeError(
                "Sorry we encountered an issue during embedding. Please try again later."
            )
        row_add_data = [
            {
                "Text": chunk.text,
                "Text Embed": text_embed,
                "Title": title,
                "Title Embed": title_embed,
                "File ID": file_info["ID"],
            }
            for chunk, text_embed in zip(chunks, text_embeds)
        ]
        await add_rows(
            request=request,
            bg_tasks=bg_tasks,
            table_type=p.TableType.knowledge,
            body=p.RowAddRequest(table_id=table_id, data=row_add_data, stream=False),
            openai_api_key=openai_api_key,
            anthropic_api_key=anthropic_api_key,
            gemini_api_key=gemini_api_key,
            cohere_api_key=cohere_api_key,
            groq_api_key=groq_api_key,
            together_api_key=together_api_key,
            jina_api_key=jina_api_key,
            voyage_api_key=voyage_api_key,
        )
        table.create_indexes(session, table_id)
    return p.OkResponse()


@router.post("/v1/gen_tables/knowledge/upload_file")
async def upload_file(
    request: Request,
    bg_tasks: BackgroundTasks,
    file: Annotated[UploadFile, File(description="The file.")],
    file_name: Annotated[str, Form(description="File name.")],
    table_id: Annotated[str, Form(description="Knowledge Table ID.")],
    # overwrite: Annotated[
    #     bool, Form(description="Whether to overwrite old file with the same name.")
    # ] = False,
    chunk_size: Annotated[
        int, Form(description="Maximum chunk size (number of characters). Must be > 0.", gt=0)
    ] = 1000,
    chunk_overlap: Annotated[
        int, Form(description="Overlap in characters between chunks. Must be >= 0.", ge=0)
    ] = 200,
    # stream: Annotated[
    #     bool, Form(description="Whether or not to stream the LLM generation.")
    # ] = True,
    openai_api_key: Annotated[str, Header(description="OpenAI API key.")] = "",
    anthropic_api_key: Annotated[str, Header(description="Anthropic API key.")] = "",
    gemini_api_key: Annotated[str, Header(description="Google Gemini API key.")] = "",
    cohere_api_key: Annotated[str, Header(description="Cohere API key.")] = "",
    groq_api_key: Annotated[str, Header(description="Groq API key.")] = "",
    together_api_key: Annotated[str, Header(description="Together AI API key.")] = "",
    jina_api_key: Annotated[str, Header(description="Jina API key.")] = "",
    voyage_api_key: Annotated[str, Header(description="Voyage API key.")] = "",
) -> p.OkResponse:
    logger.info(
        (
            f"{request.state.id} - Uploading file "
            f"(file_name={file_name}  table_id={table_id}  "
            f"chunk_size={chunk_size}  chunk_overlap={chunk_overlap})"
        )
    )
    try:
        # --- Add into File Table --- #
        content = await file.read()
        file_table = _get_file_table(request.state.org_id, request.state.project_id)
        # if overwrite:
        #     file_table.delete_file(file_name=file_name)
        # Compute checksum
        block_size = 2**10
        hasher = blake2b()
        for i in range(0, len(content), block_size):
            hasher.update(content[i : i + block_size])
        file_info = file_table.add_file(
            file_name=file_name, content=content, blake2b_checksum=hasher.hexdigest()
        )
        # --- Add into Knowledge Table --- #
        return await _add_file(
            request=request,
            bg_tasks=bg_tasks,
            request_id=request.state.id,
            table_id=table_id,
            file_info=file_info,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            openai_api_key=openai_api_key,
            anthropic_api_key=anthropic_api_key,
            gemini_api_key=gemini_api_key,
            cohere_api_key=cohere_api_key,
            groq_api_key=groq_api_key,
            together_api_key=together_api_key,
            jina_api_key=jina_api_key,
            voyage_api_key=voyage_api_key,
        )
    except OwlException:
        raise
    except Exception:
        logger.exception(
            (
                f"[{request.state.org_id}/{request.state.project_id}] "
                f"Failed to upload file into Knowledge Table: {file_name}"
            )
        )
        raise


@router.get("/v1/gen_tables/chat/{table_id}/thread")
def get_conversation_thread(
    request: Request,
    table_id: str = Path(pattern=p.TABLE_NAME_PATTERN, description="Table ID or name."),
) -> p.ChatThread:
    try:
        table: ChatTable = _get_gen_table(
            request.state.org_id, request.state.project_id, p.TableType.chat
        )
        return table.get_conversation_thread(table_id)
    except OwlException:
        raise
    except Exception:
        logger.exception(f"Failed to get conversation thread from table: {table_id}")
        raise