import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Union, final, Optional
import numpy as np
import configparser

from lightrag.types import KnowledgeGraph, KnowledgeGraphNode, KnowledgeGraphEdge

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..base import (
    BaseGraphStorage,
    BaseKVStorage,
    BaseVectorStorage,
    DocProcessingStatus,
    DocStatus,
    DocStatusStorage,
)
from ..namespace import NameSpace, is_namespace
from ..utils import logger

import pipmaster as pm

if not pm.is_installed("asyncpg"):
    pm.install("asyncpg")

import asyncpg  # type: ignore
from asyncpg import Pool  # type: ignore

# Get maximum number of graph nodes from environment variable, default is 1000
MAX_GRAPH_NODES = int(os.getenv("MAX_GRAPH_NODES", 1000))


class PostgreSQLDB:
    def __init__(self, config: dict[str, Any], **kwargs: Any):
        self.host = config.get("host", "localhost")
        self.port = config.get("port", 5432)
        self.user = config.get("user", "postgres")
        self.password = config.get("password", None)
        self.database = config.get("database", "postgres")
        self.workspace = config.get("workspace", "default")
        self.max = 12
        self.increment = 1
        self.pool: Pool | None = None

        if self.user is None or self.password is None or self.database is None:
            raise ValueError("Missing database user, password, or database")

    async def initdb(self):
        try:
            self.pool = await asyncpg.create_pool(  # type: ignore
                user=self.user,
                password=self.password,
                database=self.database,
                host=self.host,
                port=self.port,
                min_size=1,
                max_size=self.max,
            )

            logger.info(
                f"PostgreSQL, Connected to database at {self.host}:{self.port}/{self.database}"
            )
        except Exception as e:
            logger.error(
                f"PostgreSQL, Failed to connect database at {self.host}:{self.port}/{self.database}, Got:{e}"
            )
            raise

    @staticmethod
    async def configure_age(connection: asyncpg.Connection, graph_name: str) -> None:
        """Set the Apache AGE environment and creates a graph if it does not exist.

        This method:
        - Sets the PostgreSQL `search_path` to include `ag_catalog`, ensuring that Apache AGE functions can be used without specifying the schema.
        - Attempts to create a new graph with the provided `graph_name` if it does not already exist.
        - Silently ignores errors related to the graph already existing.

        """
        try:
            await connection.execute(  # type: ignore
                'SET search_path = ag_catalog, "$user", public'
            )
            await connection.execute(  # type: ignore
                f"select create_graph('{graph_name}')"
            )
        except (
            asyncpg.exceptions.InvalidSchemaNameError,
            asyncpg.exceptions.UniqueViolationError,
        ):
            pass

    async def check_tables(self):
        for k, v in TABLES.items():
            try:
                await self.query(f"SELECT 1 FROM {k} LIMIT 1")
            except Exception:
                try:
                    logger.info(f"PostgreSQL, Try Creating table {k} in database")
                    await self.execute(v["ddl"])
                    logger.info(
                        f"PostgreSQL, Creation success table {k} in PostgreSQL database"
                    )
                except Exception as e:
                    logger.error(
                        f"PostgreSQL, Failed to create table {k} in database, Please verify the connection with PostgreSQL database, Got: {e}"
                    )
                    raise e

            # Create index for id column in each table
            try:
                index_name = f"idx_{k.lower()}_id"
                check_index_sql = f"""
                SELECT 1 FROM pg_indexes
                WHERE indexname = '{index_name}'
                AND tablename = '{k.lower()}'
                """
                index_exists = await self.query(check_index_sql)

                if not index_exists:
                    create_index_sql = f"CREATE INDEX {index_name} ON {k}(id)"
                    logger.info(f"PostgreSQL, Creating index {index_name} on table {k}")
                    await self.execute(create_index_sql)
            except Exception as e:
                logger.error(
                    f"PostgreSQL, Failed to create index on table {k}, Got: {e}"
                )

    async def query(
        self,
        sql: str,
        params: dict[str, Any] | None = None,
        multirows: bool = False,
        with_age: bool = False,
        graph_name: str | None = None,
    ) -> dict[str, Any] | None | list[dict[str, Any]]:
        async with self.pool.acquire() as connection:  # type: ignore
            if with_age and graph_name:
                await self.configure_age(connection, graph_name)  # type: ignore
            elif with_age and not graph_name:
                raise ValueError("Graph name is required when with_age is True")

            try:
                if params:
                    rows = await connection.fetch(sql, *params.values())
                else:
                    rows = await connection.fetch(sql)

                if multirows:
                    if rows:
                        columns = [col for col in rows[0].keys()]
                        data = [dict(zip(columns, row)) for row in rows]
                    else:
                        data = []
                else:
                    if rows:
                        columns = rows[0].keys()
                        data = dict(zip(columns, rows[0]))
                    else:
                        data = None
                return data
            except Exception as e:
                logger.error(f"PostgreSQL database, error:{e}")
                raise

    async def execute(
        self,
        sql: str,
        data: dict[str, Any] | None = None,
        upsert: bool = False,
        with_age: bool = False,
        graph_name: str | None = None,
    ):
        try:
            async with self.pool.acquire() as connection:  # type: ignore
                if with_age and graph_name:
                    await self.configure_age(connection, graph_name)  # type: ignore
                elif with_age and not graph_name:
                    raise ValueError("Graph name is required when with_age is True")

                if data is None:
                    await connection.execute(sql)  # type: ignore
                else:
                    await connection.execute(sql, *data.values())  # type: ignore
        except (
            asyncpg.exceptions.UniqueViolationError,
            asyncpg.exceptions.DuplicateTableError,
        ) as e:
            if upsert:
                print("Key value duplicate, but upsert succeeded.")
            else:
                logger.error(f"Upsert error: {e}")
        except Exception as e:
            logger.error(f"PostgreSQL database,\nsql:{sql},\ndata:{data},\nerror:{e}")
            raise


class ClientManager:
    _instances: dict[str, Any] = {"db": None, "ref_count": 0}
    _lock = asyncio.Lock()

    @staticmethod
    def get_config() -> dict[str, Any]:
        config = configparser.ConfigParser()
        config.read("config.ini", "utf-8")

        return {
            "host": os.environ.get(
                "POSTGRES_HOST",
                config.get("postgres", "host", fallback="localhost"),
            ),
            "port": os.environ.get(
                "POSTGRES_PORT", config.get("postgres", "port", fallback=5432)
            ),
            "user": os.environ.get(
                "POSTGRES_USER", config.get("postgres", "user", fallback=None)
            ),
            "password": os.environ.get(
                "POSTGRES_PASSWORD",
                config.get("postgres", "password", fallback=None),
            ),
            "database": os.environ.get(
                "POSTGRES_DATABASE",
                config.get("postgres", "database", fallback=None),
            ),
            "workspace": os.environ.get(
                "POSTGRES_WORKSPACE",
                config.get("postgres", "workspace", fallback="default"),
            ),
        }

    @classmethod
    async def get_client(cls) -> PostgreSQLDB:
        async with cls._lock:
            if cls._instances["db"] is None:
                config = ClientManager.get_config()
                db = PostgreSQLDB(config)
                await db.initdb()
                await db.check_tables()
                cls._instances["db"] = db
                cls._instances["ref_count"] = 0
            cls._instances["ref_count"] += 1
            return cls._instances["db"]

    @classmethod
    async def release_client(cls, db: PostgreSQLDB):
        async with cls._lock:
            if db is not None:
                if db is cls._instances["db"]:
                    cls._instances["ref_count"] -= 1
                    if cls._instances["ref_count"] == 0:
                        await db.pool.close()
                        logger.info("Closed PostgreSQL database connection pool")
                        cls._instances["db"] = None
                else:
                    await db.pool.close()


@final
@dataclass
class PGKVStorage(BaseKVStorage):
    db: PostgreSQLDB = field(default=None)

    def __post_init__(self):
        self._max_batch_size = self.global_config["embedding_batch_num"]

    async def initialize(self):
        if self.db is None:
            self.db = await ClientManager.get_client()

    async def finalize(self):
        if self.db is not None:
            await ClientManager.release_client(self.db)
            self.db = None

    ################ QUERY METHODS ################

    async def get_by_id(self, id: str, workspace: str) -> dict[str, Any] | None:
        """Get doc_full data by id."""
        sql = SQL_TEMPLATES["get_by_id_" + self.namespace]
        params = {"workspace": workspace if workspace else self.db.workspace, "id": id}
        if is_namespace(self.namespace, NameSpace.KV_STORE_LLM_RESPONSE_CACHE):
            array_res = await self.db.query(sql, params, multirows=True)
            res = {}
            for row in array_res:
                res[row["id"]] = row
            return res if res else None
        else:
            response = await self.db.query(sql, params)
            return response if response else None

    async def get_by_mode_and_id(self, mode: str, id: str, workspace: str = "default") -> Union[dict, None]:
        """Specifically for llm_response_cache."""
        sql = SQL_TEMPLATES["get_by_mode_id_" + self.namespace]
        params = {"workspace": workspace if workspace else self.db.workspace, mode: mode, "id": id}
        logger.debug(f"get_by_mode_and_id:{sql}, params:{params}")
        if is_namespace(self.namespace, NameSpace.KV_STORE_LLM_RESPONSE_CACHE):
            array_res = await self.db.query(sql, params, multirows=True)
            res = {}
            for row in array_res:
                res[row["id"]] = row
            return res
        else:
            return None

    # Query by id
    async def get_by_ids(self, ids: list[str], workspace: str) -> list[dict[str, Any]]:
        """Get doc_chunks data by id"""
        sql = SQL_TEMPLATES["get_by_ids_" + self.namespace].format(
            ids=",".join([f"'{id}'" for id in ids])
        )
        params = {"workspace": workspace if workspace else self.db.workspace}
        if is_namespace(self.namespace, NameSpace.KV_STORE_LLM_RESPONSE_CACHE):
            array_res = await self.db.query(sql, params, multirows=True)
            modes = set()
            dict_res: dict[str, dict] = {}
            for row in array_res:
                modes.add(row["mode"])
            for mode in modes:
                if mode not in dict_res:
                    dict_res[mode] = {}
            for row in array_res:
                dict_res[row["mode"]][row["id"]] = row
            return [{k: v} for k, v in dict_res.items()]
        else:
            return await self.db.query(sql, params, multirows=True)

    async def get_by_status(self, status: str) -> Union[list[dict[str, Any]], None]:
        """Specifically for llm_response_cache."""
        SQL = SQL_TEMPLATES["get_by_status_" + self.namespace]
        params = {"workspace": self.db.workspace, "status": status}
        return await self.db.query(SQL, params, multirows=True)

    async def filter_keys(self, keys: set[str], workspace: str) -> set[str]:
        """Filter out duplicated content"""
        sql = SQL_TEMPLATES["filter_keys"].format(
            table_name=namespace_to_table_name(self.namespace),
            ids=",".join([f"'{id}'" for id in keys]),
        )
        params = {"workspace":  workspace if  workspace else self.db.workspace}
        try:
            res = await self.db.query(sql, params, multirows=True)
            if res:
                exist_keys = [key["id"] for key in res]
            else:
                exist_keys = []
            new_keys = set([s for s in keys if s not in exist_keys])
            return new_keys
        except Exception as e:
            logger.error(
                f"PostgreSQL database,\nsql:{sql},\nparams:{params},\nerror:{e}"
            )
            raise

    ################ INSERT METHODS ################
    async def upsert(self, data: dict[str, dict[str, Any]], workspace: str) -> None:
        logger.info(f"Inserting {len(data)} to {self.namespace}")
        if not data:
            return

        if is_namespace(self.namespace, NameSpace.KV_STORE_TEXT_CHUNKS):
            pass
        elif is_namespace(self.namespace, NameSpace.KV_STORE_FULL_DOCS):
            for k, v in data.items():
                upsert_sql = SQL_TEMPLATES["upsert_doc_full"]
                _data = {
                    "id": k,
                    "content": v["content"],
                    "workspace": workspace if workspace else self.db.workspace,
                }
                logger.debug(f"Sql:{upsert_sql}, params:{_data}")
                await self.db.execute(upsert_sql, _data)
        elif is_namespace(self.namespace, NameSpace.KV_STORE_LLM_RESPONSE_CACHE):
            for mode, items in data.items():
                for k, v in items.items():
                    upsert_sql = SQL_TEMPLATES["upsert_llm_response_cache"]
                    _data = {
                        "workspace": workspace if workspace else self.db.workspace,
                        "id": k,
                        "original_prompt": v["original_prompt"],
                        "return_value": v["return"],
                        "mode": mode,
                    }
                    logger.debug(f"Sql:{upsert_sql}, params:{_data}")
                    await self.db.execute(upsert_sql, _data)

    async def index_done_callback(self) -> None:
        # PG handles persistence automatically
        pass

    async def delete(self, ids: list[str]) -> None:
        """Delete specific records from storage by their IDs

        Args:
            ids (list[str]): List of document IDs to be deleted from storage

        Returns:
            None
        """
        if not ids:
            return

        table_name = namespace_to_table_name(self.namespace)
        if not table_name:
            logger.error(f"Unknown namespace for deletion: {self.namespace}")
            return

        delete_sql = f"DELETE FROM {table_name} WHERE workspace=$1 AND id = ANY($2)"

        try:
            await self.db.execute(
                delete_sql, {"workspace": self.db.workspace, "ids": ids}
            )
            logger.debug(
                f"Successfully deleted {len(ids)} records from {self.namespace}"
            )
        except Exception as e:
            logger.error(f"Error while deleting records from {self.namespace}: {e}")

    async def drop_cache_by_modes(self, modes: list[str] | None = None) -> bool:
        """Delete specific records from storage by cache mode

        Args:
            modes (list[str]): List of cache modes to be dropped from storage

        Returns:
            bool: True if successful, False otherwise
        """
        if not modes:
            return False

        try:
            table_name = namespace_to_table_name(self.namespace)
            if not table_name:
                return False

            if table_name != "LIGHTRAG_LLM_CACHE":
                return False

            sql = f"""
            DELETE FROM {table_name}
            WHERE workspace = $1 AND mode = ANY($2)
            """
            params = {"workspace": self.db.workspace, "modes": modes}

            logger.info(f"Deleting cache by modes: {modes}")
            await self.db.execute(sql, params)
            return True
        except Exception as e:
            logger.error(f"Error deleting cache by modes {modes}: {e}")
            return False

    async def drop(self, namespace: Optional[str] = None, workspace: str="default") -> dict[str, str]:
        """Drop the storage"""
        try:
            table_name = namespace_to_table_name(self.namespace)
            if not table_name:
                return {
                    "status": "error",
                    "message": f"Unknown namespace: {self.namespace}",
                }

            drop_sql = SQL_TEMPLATES["drop_specifiy_table_workspace"].format(
                table_name=table_name
            )
            workspace = workspace if workspace is not None else self.db.workspace
            await self.db.execute(drop_sql, {"workspace": workspace})
            return {"status": "success", "message": "data dropped"}
        except Exception as e:
            return {"status": "error", "message": str(e)}


@final
@dataclass
class PGVectorStorage(BaseVectorStorage):
    db: PostgreSQLDB | None = field(default=None)

    def __post_init__(self):
        self._max_batch_size = self.global_config["embedding_batch_num"]
        config = self.global_config.get("vector_db_storage_cls_kwargs", {})
        cosine_threshold = config.get("cosine_better_than_threshold")
        if cosine_threshold is None:
            raise ValueError(
                "cosine_better_than_threshold must be specified in vector_db_storage_cls_kwargs"
            )
        self.cosine_better_than_threshold = cosine_threshold

    async def initialize(self):
        if self.db is None:
            self.db = await ClientManager.get_client()

    async def finalize(self):
        if self.db is not None:
            await ClientManager.release_client(self.db)
            self.db = None

    def _upsert_chunks(self, item: dict[str, Any], workspace: str) -> tuple[str, dict[str, Any]]:
        try:
            upsert_sql = SQL_TEMPLATES["upsert_chunk"]
            data: dict[str, Any] = {
                "workspace": workspace if workspace else self.db.workspace,
                "id": item["__id__"],
                "tokens": item["tokens"],
                "chunk_order_index": item["chunk_order_index"],
                "full_doc_id": item["full_doc_id"],
                "content": item["content"],
                "content_vector": json.dumps(item["__vector__"].tolist()),
                "file_path": item["file_path"],
            }
        except Exception as e:
            logger.error(f"Error to prepare upsert,\nsql: {e}\nitem: {item}")
            raise

        return upsert_sql, data

    def _upsert_entities(self, item: dict[str, Any], workspace: str) -> tuple[str, dict[str, Any]]:
        upsert_sql = SQL_TEMPLATES["upsert_entity"]
        source_id = item["source_id"]
        if isinstance(source_id, str) and "<SEP>" in source_id:
            chunk_ids = source_id.split("<SEP>")
        else:
            chunk_ids = [source_id]

        data: dict[str, Any] = {
            "workspace": workspace if workspace else self.db.workspace,
            "id": item["__id__"],
            "entity_name": item["entity_name"],
            "content": item["content"],
            "content_vector": json.dumps(item["__vector__"].tolist()),
            "chunk_ids": chunk_ids,
            "file_path": item["file_path"],
            # TODO: add document_id
        }
        return upsert_sql, data

    def _upsert_relationships(self, item: dict[str, Any], workspace: str) -> tuple[str, dict[str, Any]]:
        upsert_sql = SQL_TEMPLATES["upsert_relationship"]
        source_id = item["source_id"]
        if isinstance(source_id, str) and "<SEP>" in source_id:
            chunk_ids = source_id.split("<SEP>")
        else:
            chunk_ids = [source_id]

        data: dict[str, Any] = {
            "workspace": workspace if workspace else self.db.workspace,
            "id": item["__id__"],
            "source_id": item["src_id"],
            "target_id": item["tgt_id"],
            "content": item["content"],
            "content_vector": json.dumps(item["__vector__"].tolist()),
            "chunk_ids": chunk_ids,
            "file_path": item["file_path"],
            # TODO: add document_id
        }
        return upsert_sql, data

    async def upsert(self, data: dict[str, dict[str, Any]], workspace: str) -> None:
        logger.info(f"Inserting {len(data)} to {self.namespace} with:{workspace}")
        if not data:
            return

        current_time = time.time()
        list_data = [
            {
                "__id__": k,
                "__created_at__": current_time,
                **{k1: v1 for k1, v1 in v.items()},
            }
            for k, v in data.items()
        ]
        contents = [v["content"] for v in data.values()]
        batches = [
            contents[i : i + self._max_batch_size]
            for i in range(0, len(contents), self._max_batch_size)
        ]

        embedding_tasks = [self.embedding_func(batch) for batch in batches]
        embeddings_list = await asyncio.gather(*embedding_tasks)

        embeddings = np.concatenate(embeddings_list)
        for i, d in enumerate(list_data):
            d["__vector__"] = embeddings[i]
        for item in list_data:
            if is_namespace(self.namespace, NameSpace.VECTOR_STORE_CHUNKS):
                upsert_sql, data = self._upsert_chunks(item, workspace)
            elif is_namespace(self.namespace, NameSpace.VECTOR_STORE_ENTITIES):
                upsert_sql, data = self._upsert_entities(item, workspace)
            elif is_namespace(self.namespace, NameSpace.VECTOR_STORE_RELATIONSHIPS):
                upsert_sql, data = self._upsert_relationships(item, workspace)
            else:
                raise ValueError(f"{self.namespace} is not supported")
            logger.debug(f"upset sql:{upsert_sql}, params:{data}")
            await self.db.execute(upsert_sql, data)

    #################### query method ###############
    async def query(
        self, query: str, top_k: int, ids: list[str] | None = None, workspace: str = "default"
    ) -> list[dict[str, Any]]:
        embeddings = await self.embedding_func([query])
        embedding = embeddings[0]
        embedding_string = ",".join(map(str, embedding))

        if ids:
            formatted_ids = ",".join(f"'{id}'" for id in ids)
        else:
            formatted_ids = "NULL"

        sql = SQL_TEMPLATES[self.namespace].format(
            embedding_string=embedding_string, doc_ids=formatted_ids
        )
        params = {
            "workspace": workspace if workspace else self.db.workspace,
            "better_than_threshold": self.cosine_better_than_threshold,
            "top_k": top_k,
        }
        results = await self.db.query(sql, params=params, multirows=True)
        return results

    async def index_done_callback(self) -> None:
        # PG handles persistence automatically
        pass

    async def delete(self, ids: list[str]) -> None:
        """Delete vectors with specified IDs from the storage.

        Args:
            ids: List of vector IDs to be deleted
        """
        if not ids:
            return

        table_name = namespace_to_table_name(self.namespace)
        if not table_name:
            logger.error(f"Unknown namespace for vector deletion: {self.namespace}")
            return

        delete_sql = f"DELETE FROM {table_name} WHERE workspace=$1 AND id = ANY($2)"

        try:
            await self.db.execute(
                delete_sql, {"workspace": self.db.workspace, "ids": ids}
            )
            logger.debug(
                f"Successfully deleted {len(ids)} vectors from {self.namespace}"
            )
        except Exception as e:
            logger.error(f"Error while deleting vectors from {self.namespace}: {e}")

    async def delete_entity(self, entity_name: str, workspace: str) -> None:
        """Delete an entity by its name from the vector storage.

        Args:
            entity_name: The name of the entity to delete
        """
        try:
            # Construct SQL to delete the entity
            delete_sql = """DELETE FROM LIGHTRAG_VDB_ENTITY
                            WHERE workspace=$1 AND entity_name=$2"""

            await self.db.execute(
                delete_sql, {"workspace": self.db.workspace, "entity_name": entity_name}
            )
            logger.debug(f"Successfully deleted entity {entity_name}")
        except Exception as e:
            logger.error(f"Error deleting entity {entity_name}: {e}")

    async def delete_entity_relation(self, entity_name: str) -> None:
        """Delete all relations associated with an entity.

        Args:
            entity_name: The name of the entity whose relations should be deleted
        """
        try:
            # Delete relations where the entity is either the source or target
            delete_sql = """DELETE FROM LIGHTRAG_VDB_RELATION
                            WHERE workspace=$1 AND (source_id=$2 OR target_id=$2)"""

            await self.db.execute(
                delete_sql, {"workspace": self.db.workspace, "entity_name": entity_name}
            )
            logger.debug(f"Successfully deleted relations for entity {entity_name}")
        except Exception as e:
            logger.error(f"Error deleting relations for entity {entity_name}: {e}")

    async def search_by_prefix(self, prefix: str) -> list[dict[str, Any]]:
        """Search for records with IDs starting with a specific prefix.

        Args:
            prefix: The prefix to search for in record IDs

        Returns:
            List of records with matching ID prefixes
        """
        table_name = namespace_to_table_name(self.namespace)
        if not table_name:
            logger.error(f"Unknown namespace for prefix search: {self.namespace}")
            return []

        search_sql = f"SELECT * FROM {table_name} WHERE workspace=$1 AND id LIKE $2"
        params = {"workspace": self.db.workspace, "prefix": f"{prefix}%"}

        try:
            results = await self.db.query(search_sql, params, multirows=True)
            logger.debug(f"Found {len(results)} records with prefix '{prefix}'")

            # Format results to match the expected return format
            formatted_results = []
            for record in results:
                formatted_record = dict(record)
                # Ensure id field is available (for consistency with NanoVectorDB implementation)
                if "id" not in formatted_record:
                    formatted_record["id"] = record["id"]
                formatted_results.append(formatted_record)

            return formatted_results
        except Exception as e:
            logger.error(f"Error during prefix search for '{prefix}': {e}")
            return []

    async def get_by_id(self, id: str, workspace: str) -> dict[str, Any] | None:
        """Get vector data by its ID

        Args:
            id: The unique identifier of the vector

        Returns:
            The vector data if found, or None if not found
        """
        table_name = namespace_to_table_name(self.namespace)
        if not table_name:
            logger.error(f"Unknown namespace for ID lookup: {self.namespace}")
            return None

        query = f"SELECT * FROM {table_name} WHERE workspace=$1 AND id=$2"
        params = {"workspace": workspace if workspace else self.db.workspace, "id": id}

        try:
            result = await self.db.query(query, params)
            if result:
                return dict(result)
            return None
        except Exception as e:
            logger.error(f"Error retrieving vector data for ID {id}: {e}")
            return None

    async def get_by_ids(self, ids: list[str], workspace: str) -> list[dict[str, Any]]:
        """Get multiple vector data by their IDs

        Args:
            ids: List of unique identifiers

        Returns:
            List of vector data objects that were found
        """
        if not ids:
            return []

        table_name = namespace_to_table_name(self.namespace)
        if not table_name:
            logger.error(f"Unknown namespace for IDs lookup: {self.namespace}")
            return []

        ids_str = ",".join([f"'{id}'" for id in ids])
        query = f"SELECT * FROM {table_name} WHERE workspace=$1 AND id IN ({ids_str})"
        params = {"workspace": workspace if workspace else self.db.workspace}

        try:
            results = await self.db.query(query, params, multirows=True)
            return [dict(record) for record in results]
        except Exception as e:
            logger.error(f"Error retrieving vector data for IDs {ids}: {e}")
            return []

    async def drop(self, namespace: Optional[str] = None, workspace: str="default") -> dict[str, str]:
        """Drop the storage"""
        try:
            table_name = namespace_to_table_name(self.namespace)
            if not table_name:
                return {
                    "status": "error",
                    "message": f"Unknown namespace: {self.namespace}",
                }

            drop_sql = SQL_TEMPLATES["drop_specifiy_table_workspace"].format(
                table_name=table_name
            )
            workspace = workspace if workspace is not None else self.db.workspace
            await self.db.execute(drop_sql, {"workspace": workspace})
            return {"status": "success", "message": "data dropped"}
        except Exception as e:
            return {"status": "error", "message": str(e)}


@final
@dataclass
class PGDocStatusStorage(DocStatusStorage):
    db: PostgreSQLDB = field(default=None)

    async def initialize(self):
        if self.db is None:
            self.db = await ClientManager.get_client()

    async def finalize(self):
        if self.db is not None:
            await ClientManager.release_client(self.db)
            self.db = None

    async def filter_keys(self, keys: set[str], workspace: str) -> set[str]:
        """Filter out duplicated content"""
        sql = SQL_TEMPLATES["filter_keys"].format(
            table_name=namespace_to_table_name(self.namespace),
            ids=",".join([f"'{id}'" for id in keys]),
        )

        params = {"workspace": workspace if workspace else self.db.workspace}
        # sql 日志打印
        logger.debug(f"sql:{sql}, params:{params}")
        try:
            res = await self.db.query(sql, params, multirows=True)
            if res:
                exist_keys = [key["id"] for key in res]
            else:
                exist_keys = []
            new_keys = set([s for s in keys if s not in exist_keys])
            print(f"keys: {keys}")
            print(f"new_keys: {new_keys}")
            return new_keys
        except Exception as e:
            logger.error(
                f"PostgreSQL database,\nsql:{sql},\nparams:{params},\nerror:{e}"
            )
            raise

    async def get_by_id(self, id: str, workspace: str) -> Union[dict[str, Any], None]:
        sql = "select * from LIGHTRAG_DOC_STATUS where workspace=$1 and id=$2"
        params = {"workspace": workspace if workspace else self.db.workspace, "id": id}
        result = await self.db.query(sql, params, True)
        if result is None or result == []:
            return None
        else:
            return dict(
                content=result[0]["content"],
                content_length=result[0]["content_length"],
                content_summary=result[0]["content_summary"],
                status=result[0]["status"],
                chunks_count=result[0]["chunks_count"],
                created_at=result[0]["created_at"],
                updated_at=result[0]["updated_at"],
                file_path=result[0]["file_path"],
            )

    async def get_by_ids(self, ids: list[str], workspace: str) -> list[dict[str, Any]]:
        """Get doc_chunks data by multiple IDs."""
        if not ids:
            return []

        sql = "SELECT * FROM LIGHTRAG_DOC_STATUS WHERE workspace=$1 AND id = ANY($2)"
        params = {"workspace": workspace if workspace else self.db.workspace, "ids": ids}

        results = await self.db.query(sql, params, True)

        if not results:
            return []
        return [
            {
                "content": row["content"],
                "content_length": row["content_length"],
                "content_summary": row["content_summary"],
                "status": row["status"],
                "chunks_count": row["chunks_count"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "file_path": row["file_path"],
            }
            for row in results
        ]

    async def get_status_counts(self, workspace: str = "default") -> dict[str, int]:
        """Get counts of documents in each status"""
        sql = """SELECT status as "status", COUNT(1) as "count"
                   FROM LIGHTRAG_DOC_STATUS
                  where workspace=$1 GROUP BY STATUS
                 """
        result = await self.db.query(sql, {"workspace": workspace if workspace else self.db.workspace}, True)
        counts = {}
        for doc in result:
            counts[doc["status"]] = doc["count"]
        return counts

    async def get_docs_by_status(
        self, status: DocStatus, workspace: str = "default"
    ) -> dict[str, DocProcessingStatus]:
        """all documents with a specific status"""
        sql = "select * from LIGHTRAG_DOC_STATUS where workspace=$1 and status=$2"
        params = {"workspace": workspace if workspace else self.db.workspace, "status": status.value}
        logger.debug(f"sql:{sql}, params:{params}")
        result = await self.db.query(sql, params, True)
        docs_by_status = {
            element["id"]: DocProcessingStatus(
                content=element["content"],
                content_summary=element["content_summary"],
                content_length=element["content_length"],
                status=element["status"],
                created_at=element["created_at"],
                updated_at=element["updated_at"],
                chunks_count=element["chunks_count"],
                file_path=element["file_path"],
            )
            for element in result
        }
        return docs_by_status

    async def index_done_callback(self) -> None:
        # PG handles persistence automatically
        pass

    async def delete(self, ids: list[str]) -> None:
        """Delete specific records from storage by their IDs

        Args:
            ids (list[str]): List of document IDs to be deleted from storage

        Returns:
            None
        """
        if not ids:
            return

        table_name = namespace_to_table_name(self.namespace)
        if not table_name:
            logger.error(f"Unknown namespace for deletion: {self.namespace}")
            return

        delete_sql = f"DELETE FROM {table_name} WHERE workspace=$1 AND id = ANY($2)"

        try:
            await self.db.execute(
                delete_sql, {"workspace": self.db.workspace, "ids": ids}
            )
            logger.debug(
                f"Successfully deleted {len(ids)} records from {self.namespace}"
            )
        except Exception as e:
            logger.error(f"Error while deleting records from {self.namespace}: {e}")

    async def upsert(self, data: dict[str, dict[str, Any]], workspace: str) -> None:
        """Update or insert document status

        Args:
            data: dictionary of document IDs and their status data
            workspace: workspace for documents
        """
        logger.debug(f"Inserting {len(data)} to {self.namespace}")
        if not data:
            return

        sql = """insert into LIGHTRAG_DOC_STATUS(workspace,id,content,content_summary,content_length,chunks_count,status,file_path)
                 values($1,$2,$3,$4,$5,$6,$7,$8)
                  on conflict(id,workspace) do update set
                  content = EXCLUDED.content,
                  content_summary = EXCLUDED.content_summary,
                  content_length = EXCLUDED.content_length,
                  chunks_count = EXCLUDED.chunks_count,
                  status = EXCLUDED.status,
                  file_path = EXCLUDED.file_path,
                  updated_at = CURRENT_TIMESTAMP"""
        for k, v in data.items():
            # chunks_count is optional
            await self.db.execute(
                sql,
                {
                    "workspace": workspace if workspace else self.db.workspace,
                    "id": k,
                    "content": v["content"],
                    "content_summary": v["content_summary"],
                    "content_length": v["content_length"],
                    "chunks_count": v["chunks_count"] if "chunks_count" in v else -1,
                    "status": v["status"],
                    "file_path": v["file_path"],
                },
            )

    async def drop(self, namespace: Optional[str] = None, workspace: str="default") -> dict[str, str]:
        """Drop the storage"""
        try:
            table_name = namespace_to_table_name(self.namespace)
            if not table_name:
                return {
                    "status": "error",
                    "message": f"Unknown namespace: {self.namespace}",
                }

            drop_sql = SQL_TEMPLATES["drop_specifiy_table_workspace"].format(
                table_name=table_name
            )
            workspace = workspace if workspace is not None else self.db.workspace
            await self.db.execute(drop_sql, {"workspace": workspace})
            return {"status": "success", "message": "data dropped"}
        except Exception as e:
            return {"status": "error", "message": str(e)}


class PGGraphQueryException(Exception):
    """Exception for the AGE queries."""

    def __init__(self, exception: Union[str, dict[str, Any]]) -> None:
        if isinstance(exception, dict):
            self.message = exception["message"] if "message" in exception else "unknown"
            self.details = exception["details"] if "details" in exception else "unknown"
        else:
            self.message = exception
            self.details = "unknown"

    def get_message(self) -> str:
        return self.message

    def get_details(self) -> Any:
        return self.details


@final
@dataclass
class PGGraphStorage(BaseGraphStorage):
    def __post_init__(self):
        self.graph_name = self.namespace or os.environ.get("AGE_GRAPH_NAME", "lightrag")
        self.db: PostgreSQLDB | None = None

    async def initialize(self):
        if self.db is None:
            self.db = await ClientManager.get_client()

    async def finalize(self):
        if self.db is not None:
            await ClientManager.release_client(self.db)
            self.db = None

    async def index_done_callback(self) -> None:
        # PG handles persistence automatically
        pass

    @staticmethod
    def _record_to_dict(record: asyncpg.Record) -> dict[str, Any]:
        """
        Convert a record returned from an age query to a dictionary

        Args:
            record (): a record from an age query result

        Returns:
            dict[str, Any]: a dictionary representation of the record where
                the dictionary key is the field name and the value is the
                value converted to a python type
        """
        # result holder
        d = {}

        # prebuild a mapping of vertex_id to vertex mappings to be used
        # later to build edges
        vertices = {}
        for k in record.keys():
            v = record[k]
            # agtype comes back '{key: value}::type' which must be parsed
            if isinstance(v, str) and "::" in v:
                if v.startswith("[") and v.endswith("]"):
                    if "::vertex" not in v:
                        continue
                    v = v.replace("::vertex", "")
                    vertexes = json.loads(v)
                    for vertex in vertexes:
                        vertices[vertex["id"]] = vertex.get("properties")
                else:
                    dtype = v.split("::")[-1]
                    v = v.split("::")[0]
                    if dtype == "vertex":
                        vertex = json.loads(v)
                        vertices[vertex["id"]] = vertex.get("properties")

        # iterate returned fields and parse appropriately
        for k in record.keys():
            v = record[k]
            if isinstance(v, str) and "::" in v:
                if v.startswith("[") and v.endswith("]"):
                    if "::vertex" in v:
                        v = v.replace("::vertex", "")
                        d[k] = json.loads(v)

                    elif "::edge" in v:
                        v = v.replace("::edge", "")
                        d[k] = json.loads(v)
                    else:
                        print("WARNING: unsupported type")
                        continue

                else:
                    dtype = v.split("::")[-1]
                    v = v.split("::")[0]
                    if dtype == "vertex":
                        d[k] = json.loads(v)
                    elif dtype == "edge":
                        d[k] = json.loads(v)
            else:
                try:
                    d[k] = (
                        json.loads(v)
                        if isinstance(v, str)
                        and (v.startswith("{") or v.startswith("["))
                        else v
                    )
                except json.JSONDecodeError:
                    d[k] = v

        return d

    @staticmethod
    def _format_properties(
        properties: dict[str, Any], _id: Union[str, None] = None
    ) -> str:
        """
        Convert a dictionary of properties to a string representation that
        can be used in a cypher query insert/merge statement.

        Args:
            properties (dict[str,str]): a dictionary containing node/edge properties
            _id (Union[str, None]): the id of the node or None if none exists

        Returns:
            str: the properties dictionary as a properly formatted string
        """
        props = []
        # wrap property key in backticks to escape
        for k, v in properties.items():
            prop = f"`{k}`: {json.dumps(v)}"
            props.append(prop)
        if _id is not None and "id" not in properties:
            props.append(
                f"id: {json.dumps(_id)}" if isinstance(_id, str) else f"id: {_id}"
            )
        return "{" + ", ".join(props) + "}"

    async def _query(
        self,
        query: str,
        readonly: bool = True,
        upsert: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Query the graph by taking a cypher query, converting it to an
        age compatible query, executing it and converting the result

        Args:
            query (str): a cypher query to be executed

        Returns:
            list[dict[str, Any]]: a list of dictionaries containing the result set
        """
        try:
            if readonly:
                data = await self.db.query(
                    query,
                    multirows=True,
                    with_age=True,
                    graph_name=self.graph_name,
                )
            else:
                data = await self.db.execute(
                    query,
                    upsert=upsert,
                    with_age=True,
                    graph_name=self.graph_name,
                )

        except Exception as e:
            raise PGGraphQueryException(
                {
                    "message": f"Error executing graph query: {query}",
                    "wrapped": query,
                    "detail": str(e),
                }
            ) from e

        if data is None:
            result = []
        # decode records
        else:
            result = [self._record_to_dict(d) for d in data]

        return result

    async def has_node(self, node_id: str, namespace: Optional[str] = None) -> bool:
        entity_name_label = node_id.strip('"')

        query = """SELECT * FROM cypher('%s', $$
                     MATCH (n:base {entity_id: "%s"})
                     RETURN count(n) > 0 AS node_exists
                   $$) AS (node_exists bool)""" % (self.graph_name, entity_name_label)

        single_result = (await self._query(query))[0]

        return single_result["node_exists"]

    async def has_edge(self, source_node_id: str, target_node_id: str, namespace: Optional[str] = None) -> bool:
        src_label = source_node_id.strip('"')
        tgt_label = target_node_id.strip('"')

        query = """SELECT * FROM cypher('%s', $$
                     MATCH (a:base {entity_id: "%s"})-[r]-(b:base {entity_id: "%s"})
                     RETURN COUNT(r) > 0 AS edge_exists
                   $$) AS (edge_exists bool)""" % (
            self.graph_name,
            src_label,
            tgt_label,
        )

        single_result = (await self._query(query))[0]

        return single_result["edge_exists"]

    async def get_node(self, node_id: str, namespace: Optional[str] = None) -> dict[str, str] | None:
        """Get node by its label identifier, return only node properties"""

        label = node_id.strip('"')
        query = """SELECT * FROM cypher('%s', $$
                     MATCH (n:base {entity_id: "%s"})
                     RETURN n
                   $$) AS (n agtype)""" % (self.graph_name, label)
        record = await self._query(query)
        if record:
            node = record[0]
            node_dict = node["n"]["properties"]

            return node_dict
        return None

    async def node_degree(self, node_id: str, namespace: Optional[str] = None) -> int:
        label = node_id.strip('"')

        query = """SELECT * FROM cypher('%s', $$
                     MATCH (n:base {entity_id: "%s"})-[]-(x)
                     RETURN count(x) AS total_edge_count
                   $$) AS (total_edge_count integer)""" % (self.graph_name, label)
        record = (await self._query(query))[0]
        if record:
            edge_count = int(record["total_edge_count"])
            return edge_count

    async def edge_degree(self, src_id: str, tgt_id: str, namespace: Optional[str] = None) -> int:
        src_degree = await self.node_degree(src_id)
        trg_degree = await self.node_degree(tgt_id)

        # Convert None to 0 for addition
        src_degree = 0 if src_degree is None else src_degree
        trg_degree = 0 if trg_degree is None else trg_degree

        degrees = int(src_degree) + int(trg_degree)

        return degrees

    async def get_edge(
        self, source_node_id: str, target_node_id: str, namespace: Optional[str] = None
    ) -> dict[str, str] | None:
        """Get edge properties between two nodes"""

        src_label = source_node_id.strip('"')
        tgt_label = target_node_id.strip('"')

        query = """SELECT * FROM cypher('%s', $$
                     MATCH (a:base {entity_id: "%s"})-[r]->(b:base {entity_id: "%s"})
                     RETURN properties(r) as edge_properties
                     LIMIT 1
                   $$) AS (edge_properties agtype)""" % (
            self.graph_name,
            src_label,
            tgt_label,
        )
        record = await self._query(query)
        if record and record[0] and record[0]["edge_properties"]:
            result = record[0]["edge_properties"]

            return result

    async def get_node_edges(self, source_node_id: str, namespace: Optional[str] = None) -> list[tuple[str, str]] | None:
        """
        Retrieves all edges (relationships) for a particular node identified by its label.
        :return: list of dictionaries containing edge information
        """
        label = source_node_id.strip('"')

        query = """SELECT * FROM cypher('%s', $$
                      MATCH (n:base {entity_id: "%s"})
                      OPTIONAL MATCH (n)-[]-(connected:base)
                      RETURN n, connected
                    $$) AS (n agtype, connected agtype)""" % (
            self.graph_name,
            label,
        )

        results = await self._query(query)
        edges = []
        for record in results:
            source_node = record["n"] if record["n"] else None
            connected_node = record["connected"] if record["connected"] else None

            if (
                source_node
                and connected_node
                and "properties" in source_node
                and "properties" in connected_node
            ):
                source_label = source_node["properties"].get("entity_id")
                target_label = connected_node["properties"].get("entity_id")

                if source_label and target_label:
                    edges.append((source_label, target_label))

        return edges

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type((PGGraphQueryException,)),
    )
    async def upsert_node(self, node_id: str, node_data: dict[str, str], namespace: Optional[str] = None) -> None:
        """
        Upsert a node in the Neo4j database.

        Args:
            node_id: The unique identifier for the node (used as label)
            node_data: Dictionary of node properties
        """
        if "entity_id" not in node_data:
            raise ValueError(
                "PostgreSQL: node properties must contain an 'entity_id' field"
            )

        label = node_id.strip('"')
        properties = self._format_properties(node_data)

        query = """SELECT * FROM cypher('%s', $$
                     MERGE (n:base {entity_id: "%s"})
                     SET n += %s
                     RETURN n
                   $$) AS (n agtype)""" % (
            self.graph_name,
            label,
            properties,
        )

        try:
            await self._query(query, readonly=False, upsert=True)

        except Exception:
            logger.error(f"POSTGRES, upsert_node error on node_id: `{node_id}`")
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=4, max=10),
        retry=retry_if_exception_type((PGGraphQueryException,)),
    )
    async def upsert_edge(
        self, source_node_id: str, target_node_id: str, edge_data: dict[str, str], namespace: Optional[str] = None
    ) -> None:
        """
        Upsert an edge and its properties between two nodes identified by their labels.

        Args:
            source_node_id (str): Label of the source node (used as identifier)
            target_node_id (str): Label of the target node (used as identifier)
            edge_data (dict): dictionary of properties to set on the edge
        """
        src_label = source_node_id.strip('"')
        tgt_label = target_node_id.strip('"')
        edge_properties = self._format_properties(edge_data)

        query = """SELECT * FROM cypher('%s', $$
                     MATCH (source:base {entity_id: "%s"})
                     WITH source
                     MATCH (target:base {entity_id: "%s"})
                     MERGE (source)-[r:DIRECTED]->(target)
                     SET r += %s
                     RETURN r
                   $$) AS (r agtype)""" % (
            self.graph_name,
            src_label,
            tgt_label,
            edge_properties,
        )

        try:
            await self._query(query, readonly=False, upsert=True)

        except Exception:
            logger.error(
                f"POSTGRES, upsert_edge error on edge: `{source_node_id}`-`{target_node_id}`"
            )
            raise

    async def delete_node(self, node_id: str) -> None:
        """
        Delete a node from the graph.

        Args:
            node_id (str): The ID of the node to delete.
        """
        label = node_id.strip('"')

        query = """SELECT * FROM cypher('%s', $$
                     MATCH (n:base {entity_id: "%s"})
                     DETACH DELETE n
                   $$) AS (n agtype)""" % (self.graph_name, label)

        try:
            await self._query(query, readonly=False)
        except Exception as e:
            logger.error("Error during node deletion: {%s}", e)
            raise

    async def remove_nodes(self, node_ids: list[str], namespace: Optional[str] = None) -> None:
        """
        Remove multiple nodes from the graph.

        Args:
            node_ids (list[str]): A list of node IDs to remove.
        """
        node_ids = [node_id.strip('"') for node_id in node_ids]
        node_id_list = ", ".join([f'"{node_id}"' for node_id in node_ids])

        query = """SELECT * FROM cypher('%s', $$
                     MATCH (n:base)
                     WHERE n.entity_id IN [%s]
                     DETACH DELETE n
                   $$) AS (n agtype)""" % (self.graph_name, node_id_list)

        try:
            await self._query(query, readonly=False)
        except Exception as e:
            logger.error("Error during node removal: {%s}", e)
            raise

    async def remove_edges(self, edges: list[tuple[str, str]], namespace: Optional[str] = None) -> None:
        """
        Remove multiple edges from the graph.

        Args:
            edges (list[tuple[str, str]]): A list of edges to remove, where each edge is a tuple of (source_node_id, target_node_id).
        """
        for source, target in edges:
            src_label = source.strip('"')
            tgt_label = target.strip('"')

            query = """SELECT * FROM cypher('%s', $$
                         MATCH (a:base {entity_id: "%s"})-[r]->(b:base {entity_id: "%s"})
                         DELETE r
                       $$) AS (r agtype)""" % (self.graph_name, src_label, tgt_label)

            try:
                await self._query(query, readonly=False)
                logger.debug(f"Deleted edge from '{source}' to '{target}'")
            except Exception as e:
                logger.error(f"Error during edge deletion: {str(e)}")
                raise

    async def get_all_labels(self, namespace: Optional[str] = None) -> list[str]:
        """
        Get all labels (node IDs) in the graph.

        Returns:
            list[str]: A list of all labels in the graph.
        """
        query = (
            """SELECT * FROM cypher('%s', $$
                     MATCH (n:base)
                     WHERE n.entity_id IS NOT NULL
                     RETURN DISTINCT n.entity_id AS label
                     ORDER BY n.entity_id
                   $$) AS (label text)"""
            % self.graph_name
        )

        results = await self._query(query)
        labels = [result["label"] for result in results]
        return labels

    async def get_knowledge_graph(
        self,
        node_label: str,
        max_depth: int = 3,
        max_nodes: int = MAX_GRAPH_NODES, namespace: Optional[str] = None
    ) -> KnowledgeGraph:
        """
        Retrieve a connected subgraph of nodes where the label includes the specified `node_label`.

        Args:
            node_label: Label of the starting node, * means all nodes
            max_depth: Maximum depth of the subgraph, Defaults to 3
            max_nodes: Maxiumu nodes to return, Defaults to 1000 (not BFS nor DFS garanteed)

        Returns:
            KnowledgeGraph object containing nodes and edges, with an is_truncated flag
            indicating whether the graph was truncated due to max_nodes limit
        """
        # First, count the total number of nodes that would be returned without limit
        if node_label == "*":
            count_query = f"""SELECT * FROM cypher('{self.graph_name}', $$
                    MATCH (n:base)
                    RETURN count(distinct n) AS total_nodes
                    $$) AS (total_nodes bigint)"""
        else:
            strip_label = node_label.strip('"')
            count_query = f"""SELECT * FROM cypher('{self.graph_name}', $$
                    MATCH (n:base {{entity_id: "{strip_label}"}})
                    OPTIONAL MATCH p = (n)-[*..{max_depth}]-(m)
                    RETURN count(distinct m) AS total_nodes
                    $$) AS (total_nodes bigint)"""

        count_result = await self._query(count_query)
        total_nodes = count_result[0]["total_nodes"] if count_result else 0
        is_truncated = total_nodes > max_nodes

        # Now get the actual data with limit
        if node_label == "*":
            query = f"""SELECT * FROM cypher('{self.graph_name}', $$
                    MATCH (n:base)
                    OPTIONAL MATCH (n)-[r]->(target:base)
                    RETURN collect(distinct n) AS n, collect(distinct r) AS r
                    LIMIT {max_nodes}
                    $$) AS (n agtype, r agtype)"""
        else:
            strip_label = node_label.strip('"')
            query = f"""SELECT * FROM cypher('{self.graph_name}', $$
                    MATCH (n:base {{entity_id: "{strip_label}"}})
                    OPTIONAL MATCH p = (n)-[*..{max_depth}]-(m)
                    RETURN nodes(p) AS n, relationships(p) AS r
                    LIMIT {max_nodes}
                    $$) AS (n agtype, r agtype)"""

        results = await self._query(query)

        # Process the query results with deduplication by node and edge IDs
        nodes_dict = {}
        edges_dict = {}
        for result in results:
            # Handle single node cases
            if result.get("n") and isinstance(result["n"], dict):
                node_id = str(result["n"]["id"])
                if node_id not in nodes_dict:
                    nodes_dict[node_id] = KnowledgeGraphNode(
                        id=node_id,
                        labels=[result["n"]["properties"]["entity_id"]],
                        properties=result["n"]["properties"],
                    )
            # Handle node list cases
            elif result.get("n") and isinstance(result["n"], list):
                for node in result["n"]:
                    if isinstance(node, dict) and "id" in node:
                        node_id = str(node["id"])
                        if node_id not in nodes_dict and "properties" in node:
                            nodes_dict[node_id] = KnowledgeGraphNode(
                                id=node_id,
                                labels=[node["properties"]["entity_id"]],
                                properties=node["properties"],
                            )

            # Handle single edge cases
            if result.get("r") and isinstance(result["r"], dict):
                edge_id = str(result["r"]["id"])
                if edge_id not in edges_dict:
                    edges_dict[edge_id] = KnowledgeGraphEdge(
                        id=edge_id,
                        type="DIRECTED",
                        source=str(result["r"]["start_id"]),
                        target=str(result["r"]["end_id"]),
                        properties=result["r"]["properties"],
                    )
            # Handle edge list cases
            elif result.get("r") and isinstance(result["r"], list):
                for edge in result["r"]:
                    if isinstance(edge, dict) and "id" in edge:
                        edge_id = str(edge["id"])
                        if edge_id not in edges_dict:
                            edges_dict[edge_id] = KnowledgeGraphEdge(
                                id=edge_id,
                                type="DIRECTED",
                                source=str(edge["start_id"]),
                                target=str(edge["end_id"]),
                                properties=edge["properties"],
                            )

        # Construct and return the KnowledgeGraph with deduplicated nodes and edges
        kg = KnowledgeGraph(
            nodes=list(nodes_dict.values()),
            edges=list(edges_dict.values()),
            is_truncated=is_truncated,
        )

        logger.info(
            f"Subgraph query successful | Node count: {len(kg.nodes)} | Edge count: {len(kg.edges)}"
        )
        return kg

    async def drop(self, namespace: Optional[str] = None, workspace: str="default") -> dict[str, str]:
        """Drop the storage"""
        try:
            drop_query = f"""SELECT * FROM cypher('{self.graph_name}', $$
                              MATCH (n)
                              DETACH DELETE n
                            $$) AS (result agtype)"""

            await self._query(drop_query, readonly=False)
            return {"status": "success", "message": "graph data dropped"}
        except Exception as e:
            logger.error(f"Error dropping graph: {e}")
            return {"status": "error", "message": str(e)}


NAMESPACE_TABLE_MAP = {
    NameSpace.KV_STORE_FULL_DOCS: "LIGHTRAG_DOC_FULL",
    NameSpace.KV_STORE_TEXT_CHUNKS: "LIGHTRAG_DOC_CHUNKS",
    NameSpace.VECTOR_STORE_CHUNKS: "LIGHTRAG_DOC_CHUNKS",
    NameSpace.VECTOR_STORE_ENTITIES: "LIGHTRAG_VDB_ENTITY",
    NameSpace.VECTOR_STORE_RELATIONSHIPS: "LIGHTRAG_VDB_RELATION",
    NameSpace.DOC_STATUS: "LIGHTRAG_DOC_STATUS",
    NameSpace.KV_STORE_LLM_RESPONSE_CACHE: "LIGHTRAG_LLM_CACHE",
}


def namespace_to_table_name(namespace: str) -> str:
    for k, v in NAMESPACE_TABLE_MAP.items():
        if is_namespace(namespace, k):
            return v


TABLES = {
    "LIGHTRAG_DOC_FULL": {
        "ddl": """CREATE TABLE LIGHTRAG_DOC_FULL (
                    id VARCHAR(255),
                    workspace VARCHAR(255),
                    doc_name VARCHAR(1024),
                    content TEXT,
                    meta JSONB,
                    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    update_time TIMESTAMP,
	                CONSTRAINT LIGHTRAG_DOC_FULL_PK PRIMARY KEY (workspace, id)
                    )"""
    },
    "LIGHTRAG_DOC_CHUNKS": {
        "ddl": """CREATE TABLE LIGHTRAG_DOC_CHUNKS (
                    id VARCHAR(255),
                    workspace VARCHAR(255),
                    full_doc_id VARCHAR(256),
                    chunk_order_index INTEGER,
                    tokens INTEGER,
                    content TEXT,
                    content_vector VECTOR,
                    file_path VARCHAR(256),
                    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    update_time TIMESTAMP,
	                CONSTRAINT LIGHTRAG_DOC_CHUNKS_PK PRIMARY KEY (workspace, id)
                    )"""
    },
    "LIGHTRAG_VDB_ENTITY": {
        "ddl": """CREATE TABLE LIGHTRAG_VDB_ENTITY (
                    id VARCHAR(255),
                    workspace VARCHAR(255),
                    entity_name VARCHAR(255),
                    content TEXT,
                    content_vector VECTOR,
                    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    update_time TIMESTAMP,
                    chunk_ids VARCHAR(255)[] NULL,
                    file_path TEXT NULL,
	                CONSTRAINT LIGHTRAG_VDB_ENTITY_PK PRIMARY KEY (workspace, id)
                    )"""
    },
    "LIGHTRAG_VDB_RELATION": {
        "ddl": """CREATE TABLE LIGHTRAG_VDB_RELATION (
                    id VARCHAR(255),
                    workspace VARCHAR(255),
                    source_id VARCHAR(256),
                    target_id VARCHAR(256),
                    content TEXT,
                    content_vector VECTOR,
                    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    update_time TIMESTAMP,
                    chunk_ids VARCHAR(255)[] NULL,
                    file_path TEXT NULL,
	                CONSTRAINT LIGHTRAG_VDB_RELATION_PK PRIMARY KEY (workspace, id)
                    )"""
    },
    "LIGHTRAG_LLM_CACHE": {
        "ddl": """CREATE TABLE LIGHTRAG_LLM_CACHE (
	                workspace varchar(255) NOT NULL,
	                id varchar(255) NOT NULL,
	                mode varchar(32) NOT NULL,
                    original_prompt TEXT,
                    return_value TEXT,
                    create_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    update_time TIMESTAMP,
	                CONSTRAINT LIGHTRAG_LLM_CACHE_PK PRIMARY KEY (workspace, mode, id)
                    )"""
    },
    "LIGHTRAG_DOC_STATUS": {
        "ddl": """CREATE TABLE LIGHTRAG_DOC_STATUS (
	               workspace varchar(255) NOT NULL,
	               id varchar(255) NOT NULL,
	               content TEXT NULL,
	               content_summary varchar(255) NULL,
	               content_length int4 NULL,
	               chunks_count int4 NULL,
	               status varchar(64) NULL,
	               file_path TEXT NULL,
	               created_at timestamp DEFAULT CURRENT_TIMESTAMP NULL,
	               updated_at timestamp DEFAULT CURRENT_TIMESTAMP NULL,
	               CONSTRAINT LIGHTRAG_DOC_STATUS_PK PRIMARY KEY (workspace, id)
	              )"""
    },
}


SQL_TEMPLATES = {
    # SQL for KVStorage
    "get_by_id_full_docs": """SELECT id, COALESCE(content, '') as content
                                FROM LIGHTRAG_DOC_FULL WHERE workspace=$1 AND id=$2
                            """,
    "get_by_id_text_chunks": """SELECT id, tokens, COALESCE(content, '') as content,
                                chunk_order_index, full_doc_id, file_path
                                FROM LIGHTRAG_DOC_CHUNKS WHERE workspace=$1 AND id=$2
                            """,
    "get_by_id_llm_response_cache": """SELECT id, original_prompt, COALESCE(return_value, '') as "return", mode
                                FROM LIGHTRAG_LLM_CACHE WHERE workspace=$1 AND mode=$2
                               """,
    "get_by_mode_id_llm_response_cache": """SELECT id, original_prompt, COALESCE(return_value, '') as "return", mode
                           FROM LIGHTRAG_LLM_CACHE WHERE workspace=$1 AND mode=$2 AND id=$3
                          """,
    "get_by_ids_full_docs": """SELECT id, COALESCE(content, '') as content
                                 FROM LIGHTRAG_DOC_FULL WHERE workspace=$1 AND id IN ({ids})
                            """,
    "get_by_ids_text_chunks": """SELECT id, tokens, COALESCE(content, '') as content,
                                  chunk_order_index, full_doc_id, file_path
                                   FROM LIGHTRAG_DOC_CHUNKS WHERE workspace=$1 AND id IN ({ids})
                                """,
    "get_by_ids_llm_response_cache": """SELECT id, original_prompt, COALESCE(return_value, '') as "return", mode
                                 FROM LIGHTRAG_LLM_CACHE WHERE workspace=$1 AND mode= IN ({ids})
                                """,
    "filter_keys": "SELECT id FROM {table_name} WHERE workspace=$1 AND id IN ({ids})",
    "upsert_doc_full": """INSERT INTO LIGHTRAG_DOC_FULL (id, content, workspace)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (workspace,id) DO UPDATE
                           SET content = $2, update_time = CURRENT_TIMESTAMP
                       """,
    "upsert_llm_response_cache": """INSERT INTO LIGHTRAG_LLM_CACHE(workspace,id,original_prompt,return_value,mode)
                                      VALUES ($1, $2, $3, $4, $5)
                                      ON CONFLICT (workspace,mode,id) DO UPDATE
                                      SET original_prompt = EXCLUDED.original_prompt,
                                      return_value=EXCLUDED.return_value,
                                      mode=EXCLUDED.mode,
                                      update_time = CURRENT_TIMESTAMP
                                     """,
    "upsert_chunk": """INSERT INTO LIGHTRAG_DOC_CHUNKS (workspace, id, tokens,
                      chunk_order_index, full_doc_id, content, content_vector, file_path)
                      VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                      ON CONFLICT (workspace,id) DO UPDATE
                      SET tokens=EXCLUDED.tokens,
                      chunk_order_index=EXCLUDED.chunk_order_index,
                      full_doc_id=EXCLUDED.full_doc_id,
                      content = EXCLUDED.content,
                      content_vector=EXCLUDED.content_vector,
                      file_path=EXCLUDED.file_path,
                      update_time = CURRENT_TIMESTAMP
                     """,
    # SQL for VectorStorage
    "upsert_entity": """INSERT INTO LIGHTRAG_VDB_ENTITY (workspace, id, entity_name, content,
                      content_vector, chunk_ids, file_path)
                      VALUES ($1, $2, $3, $4, $5, $6::varchar[], $7)
                      ON CONFLICT (workspace,id) DO UPDATE
                      SET entity_name=EXCLUDED.entity_name,
                      content=EXCLUDED.content,
                      content_vector=EXCLUDED.content_vector,
                      chunk_ids=EXCLUDED.chunk_ids,
                      file_path=EXCLUDED.file_path,
                      update_time=CURRENT_TIMESTAMP
                     """,
    "upsert_relationship": """INSERT INTO LIGHTRAG_VDB_RELATION (workspace, id, source_id,
                      target_id, content, content_vector, chunk_ids, file_path)
                      VALUES ($1, $2, $3, $4, $5, $6, $7::varchar[], $8)
                      ON CONFLICT (workspace,id) DO UPDATE
                      SET source_id=EXCLUDED.source_id,
                      target_id=EXCLUDED.target_id,
                      content=EXCLUDED.content,
                      content_vector=EXCLUDED.content_vector,
                      chunk_ids=EXCLUDED.chunk_ids,
                      file_path=EXCLUDED.file_path,
                      update_time = CURRENT_TIMESTAMP
                     """,
    "relationships": """
    WITH relevant_chunks AS (
        SELECT id as chunk_id
        FROM LIGHTRAG_DOC_CHUNKS
        WHERE {doc_ids} IS NULL OR full_doc_id = ANY(ARRAY[{doc_ids}])
    )
    SELECT source_id as src_id, target_id as tgt_id
    FROM (
        SELECT r.id, r.source_id, r.target_id, 1 - (r.content_vector <=> '[{embedding_string}]'::vector) as distance
        FROM LIGHTRAG_VDB_RELATION r
        JOIN relevant_chunks c ON c.chunk_id = ANY(r.chunk_ids)
        WHERE r.workspace=$1
    ) filtered
    WHERE distance>$2
    ORDER BY distance DESC
    LIMIT $3
    """,
    "entities": """
        WITH relevant_chunks AS (
            SELECT id as chunk_id
            FROM LIGHTRAG_DOC_CHUNKS
            WHERE {doc_ids} IS NULL OR full_doc_id = ANY(ARRAY[{doc_ids}])
        )
        SELECT entity_name FROM
            (
                SELECT e.id, e.entity_name, 1 - (e.content_vector <=> '[{embedding_string}]'::vector) as distance
                FROM LIGHTRAG_VDB_ENTITY e
                JOIN relevant_chunks c ON c.chunk_id = ANY(e.chunk_ids)
                WHERE e.workspace=$1
            )
        WHERE distance>$2
        ORDER BY distance DESC
        LIMIT $3
    """,
    "chunks": """
        WITH relevant_chunks AS (
            SELECT id as chunk_id
            FROM LIGHTRAG_DOC_CHUNKS
            WHERE {doc_ids} IS NULL OR full_doc_id = ANY(ARRAY[{doc_ids}])
        )
        SELECT id, content, file_path FROM
            (
                SELECT id, content, file_path, 1 - (content_vector <=> '[{embedding_string}]'::vector) as distance
                FROM LIGHTRAG_DOC_CHUNKS
                where workspace=$1
                AND id IN (SELECT chunk_id FROM relevant_chunks)
            ) as chunk_distances
            WHERE distance>$2
            ORDER BY distance DESC
            LIMIT $3
    """,
    # DROP tables
    "drop_specifiy_table_workspace": """
        DELETE FROM {table_name} WHERE workspace=$1
       """,
}
