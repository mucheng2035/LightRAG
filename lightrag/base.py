from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
import os
from dotenv import load_dotenv
from dataclasses import dataclass, field
from typing import (
    Any,
    Literal,
    TypedDict,
    TypeVar,
    Callable,
    Optional,
)
from .utils import EmbeddingFunc
from .types import KnowledgeGraph

# use the .env that is inside the current folder
# allows to use different .env file for each lightrag instance
# the OS environment variables take precedence over the .env file
load_dotenv(dotenv_path=".env", override=False)


class TextChunkSchema(TypedDict):
    tokens: int
    content: str
    full_doc_id: str
    chunk_order_index: int


T = TypeVar("T")


@dataclass
class QueryParam:
    """Configuration parameters for query execution in LightRAG."""
    workspace: str =  "default"
    """ document work dir"""

    namespace: str = None
    """neo4j database"""

    mode: Literal["local", "global", "hybrid", "naive", "mix", "bypass"] = "global"
    """Specifies the retrieval mode:
    - "local": Focuses on context-dependent information.
    - "global": Utilizes global knowledge.
    - "hybrid": Combines local and global retrieval methods.
    - "naive": Performs a basic search without advanced techniques.
    - "mix": Integrates knowledge graph and vector retrieval.
    """

    only_need_context: bool = False
    """If True, only returns the retrieved context without generating a response."""

    only_need_prompt: bool = False
    """If True, only returns the generated prompt without producing a response."""

    response_type: str = "Multiple Paragraphs"
    """Defines the response format. Examples: 'Multiple Paragraphs', 'Single Paragraph', 'Bullet Points'."""

    stream: bool = False
    """If True, enables streaming output for real-time responses."""

    top_k: int = int(os.getenv("TOP_K", "60"))
    """Number of top items to retrieve. Represents entities in 'local' mode and relationships in 'global' mode."""

    max_token_for_text_unit: int = int(os.getenv("MAX_TOKEN_TEXT_CHUNK", "4000"))
    """Maximum number of tokens allowed for each retrieved text chunk."""

    max_token_for_global_context: int = int(
        os.getenv("MAX_TOKEN_RELATION_DESC", "4000")
    )
    """Maximum number of tokens allocated for relationship descriptions in global retrieval."""

    max_token_for_local_context: int = int(os.getenv("MAX_TOKEN_ENTITY_DESC", "4000"))
    """Maximum number of tokens allocated for entity descriptions in local retrieval."""

    hl_keywords: list[str] = field(default_factory=list)
    """List of high-level keywords to prioritize in retrieval."""

    ll_keywords: list[str] = field(default_factory=list)
    """List of low-level keywords to refine retrieval focus."""

    conversation_history: list[dict[str, str]] = field(default_factory=list)
    """Stores past conversation history to maintain context.
    Format: [{"role": "user/assistant", "content": "message"}].
    """

    history_turns: int = 3
    """Number of complete conversation turns (user-assistant pairs) to consider in the response context."""

    ids: list[str] | None = None
    """List of ids to filter the results."""

    model_func: Callable[..., object] | None = None
    """Optional override for the LLM model function to use for this specific query.
    If provided, this will be used instead of the global model function.
    This allows using different models for different query modes.
    """


@dataclass
class StorageNameSpace(ABC):
    namespace: str
    global_config: dict[str, Any]

    async def initialize(self):
        """Initialize the storage"""
        pass

    async def finalize(self):
        """Finalize the storage"""
        pass

    @abstractmethod
    async def index_done_callback(self) -> None:
        """Commit the storage operations after indexing"""

    @abstractmethod
    async def drop(self, namespace: Optional[str] = None, workspace: str="default") -> dict[str, str]:
        """Drop all data from storage and clean up resources

        This abstract method defines the contract for dropping all data from a storage implementation.
        Each storage type must implement this method to:
        1. Clear all data from memory and/or external storage
        2. Remove any associated storage files if applicable
        3. Reset the storage to its initial state
        4. Handle cleanup of any resources
        5. Notify other processes if necessary
        6. This action should persistent the data to disk immediately.

        Returns:
            dict[str, str]: Operation status and message with the following format:
                {
                    "status": str,  # "success" or "error"
                    "message": str  # "data dropped" on success, error details on failure
                }

        Implementation specific:
        - On success: return {"status": "success", "message": "data dropped"}
        - On failure: return {"status": "error", "message": "<error details>"}
        - If not supported: return {"status": "error", "message": "unsupported"}
        """


@dataclass
class BaseVectorStorage(StorageNameSpace, ABC):
    embedding_func: EmbeddingFunc
    cosine_better_than_threshold: float = field(default=0.2)
    meta_fields: set[str] = field(default_factory=set)

    @abstractmethod
    async def query(
        self, query: str, top_k: int, ids: list[str] | None = None, workspace: str = "default"
    ) -> list[dict[str, Any]]:
        """Query the vector storage and retrieve top_k results."""

    @abstractmethod
    async def upsert(self, data: dict[str, dict[str, Any]], workspace: str) -> None:
        """Insert or update vectors in the storage.

        Importance notes for in-memory storage:
        1. Changes will be persisted to disk during the next index_done_callback
        2. Only one process should updating the storage at a time before index_done_callback,
           KG-storage-log should be used to avoid data corruption
        """

    @abstractmethod
    async def delete_entity(self, entity_name: str, workspace: str) -> None:
        """Delete a single entity by its name.

        Importance notes for in-memory storage:
        1. Changes will be persisted to disk during the next index_done_callback
        2. Only one process should updating the storage at a time before index_done_callback,
           KG-storage-log should be used to avoid data corruption
        """

    @abstractmethod
    async def delete_entity_relation(self, entity_name: str, workspace: str) -> None:
        """Delete relations for a given entity.

        Importance notes for in-memory storage:
        1. Changes will be persisted to disk during the next index_done_callback
        2. Only one process should updating the storage at a time before index_done_callback,
           KG-storage-log should be used to avoid data corruption
        """

    @abstractmethod
    async def get_by_id(self, id: str, workspace: str) -> dict[str, Any] | None:
        """Get vector data by its ID

        Args:
            id: The unique identifier of the vector

        Returns:
            The vector data if found, or None if not found
        """
        pass

    @abstractmethod
    async def get_by_ids(self, ids: list[str], workspace: str) -> list[dict[str, Any]]:
        """Get multiple vector data by their IDs

        Args:
            ids: List of unique identifiers

        Returns:
            List of vector data objects that were found
        """
        pass

    @abstractmethod
    async def delete(self, ids: list[str]):
        """Delete vectors with specified IDs

        Importance notes for in-memory storage:
        1. Changes will be persisted to disk during the next index_done_callback
        2. Only one process should updating the storage at a time before index_done_callback,
           KG-storage-log should be used to avoid data corruption

        Args:
            ids: List of vector IDs to be deleted
        """


@dataclass
class BaseKVStorage(StorageNameSpace, ABC):
    embedding_func: EmbeddingFunc

    @abstractmethod
    async def get_by_id(self, id: str, workspace: str) -> dict[str, Any] | None:
        """Get value by id"""

    @abstractmethod
    async def get_by_ids(self, ids: list[str], workspace: str) -> list[dict[str, Any]]:
        """Get values by ids"""

    @abstractmethod
    async def filter_keys(self, keys: set[str], workspace: str) -> set[str]:
        """Return un-exist keys"""

    @abstractmethod
    async def upsert(self, data: dict[str, dict[str, Any]], workspace: str) -> None:
        """Upsert data

        Importance notes for in-memory storage:
        1. Changes will be persisted to disk during the next index_done_callback
        2. update flags to notify other processes that data persistence is needed
        """

    @abstractmethod
    async def delete(self, ids: list[str]) -> None:
        """Delete specific records from storage by their IDs

        Importance notes for in-memory storage:
        1. Changes will be persisted to disk during the next index_done_callback
        2. update flags to notify other processes that data persistence is needed

        Args:
            ids (list[str]): List of document IDs to be deleted from storage

        Returns:
            None
        """

    async def drop_cache_by_modes(self, modes: list[str] | None = None) -> bool:
        """Delete specific records from storage by cache mode

        Importance notes for in-memory storage:
        1. Changes will be persisted to disk during the next index_done_callback
        2. update flags to notify other processes that data persistence is needed

        Args:
            modes (list[str]): List of cache modes to be dropped from storage

        Returns:
             True: if the cache drop successfully
             False: if the cache drop failed, or the cache mode is not supported
        """


@dataclass
class BaseGraphStorage(StorageNameSpace, ABC):
    embedding_func: EmbeddingFunc

    @abstractmethod
    async def has_node(self, node_id: str, namespace: Optional[str] = None) -> bool:
        """Check if a node exists in the graph.

        Args:
            node_id: The ID of the node to check
            namespace: namespace for data

        Returns:
            True if the node exists, False otherwise
        """

    @abstractmethod
    async def has_edge(self, source_node_id: str, target_node_id: str, namespace: Optional[str] = None) -> bool:
        """Check if an edge exists between two nodes.

        Args:
            source_node_id: The ID of the source node
            target_node_id: The ID of the target node
            namespace: namespace for data

        Returns:
            True if the edge exists, False otherwise
        """

    @abstractmethod
    async def node_degree(self, node_id: str, namespace: Optional[str] = None) -> int:
        """Get the degree (number of connected edges) of a node.

        Args:
            node_id: The ID of the node
            namespace: namespace for data
        Returns:
            The number of edges connected to the node
        """

    @abstractmethod
    async def edge_degree(self, src_id: str, tgt_id: str, namespace: Optional[str] = None) -> int:
        """Get the total degree of an edge (sum of degrees of its source and target nodes).

        Args:
            src_id: The ID of the source node
            tgt_id: The ID of the target node
            namespace: namespace for data
        Returns:
            The sum of the degrees of the source and target nodes
        """

    @abstractmethod
    async def get_node(self, node_id: str, namespace: Optional[str] = None) -> dict[str, str] | None:
        """Get node by its ID, returning only node properties.

        Args:
            node_id: The ID of the node to retrieve
            namespace: namespace for data
        Returns:
            A dictionary of node properties if found, None otherwise
        """

    @abstractmethod
    async def get_edge(
        self, source_node_id: str, target_node_id: str, namespace: Optional[str] = None
    ) -> dict[str, str] | None:
        """Get edge properties between two nodes.

        Args:
            source_node_id: The ID of the source node
            target_node_id: The ID of the target node
            namespace: namespace for data
        Returns:
            A dictionary of edge properties if found, None otherwise
        """

    @abstractmethod
    async def get_node_edges(self, source_node_id: str, namespace: Optional[str] = None) -> list[tuple[str, str]] | None:
        """Get all edges connected to a node.

        Args:
            source_node_id: The ID of the node to get edges for
            namespace: namespace for data
        Returns:
            A list of (source_id, target_id) tuples representing edges,
            or None if the node doesn't exist
        """

    @abstractmethod
    async def upsert_node(self, node_id: str, node_data: dict[str, str], namespace: Optional[str] = None) -> None:
        """Insert a new node or update an existing node in the graph.

        Importance notes for in-memory storage:
        1. Changes will be persisted to disk during the next index_done_callback
        2. Only one process should updating the storage at a time before index_done_callback,
           KG-storage-log should be used to avoid data corruption

        Args:
            node_id: The ID of the node to insert or update
            node_data: A dictionary of node properties
        """

    @abstractmethod
    async def upsert_edge(
        self, source_node_id: str, target_node_id: str, edge_data: dict[str, str], namespace: Optional[str] = None
    ) -> None:
        """Insert a new edge or update an existing edge in the graph.

        Importance notes for in-memory storage:
        1. Changes will be persisted to disk during the next index_done_callback
        2. Only one process should updating the storage at a time before index_done_callback,
           KG-storage-log should be used to avoid data corruption

        Args:
            source_node_id: The ID of the source node
            target_node_id: The ID of the target node
            edge_data: A dictionary of edge properties
            namespace: namespace for data
        """

    @abstractmethod
    async def delete_node(self, node_id: str, namespace: Optional[str] = None) -> None:
        """Delete a node from the graph.

        Importance notes for in-memory storage:
        1. Changes will be persisted to disk during the next index_done_callback
        2. Only one process should updating the storage at a time before index_done_callback,
           KG-storage-log should be used to avoid data corruption

        Args:
            node_id: The ID of the node to delete
            namespace: namespace for data
        """

    @abstractmethod
    async def remove_nodes(self, nodes: list[str], namespace: Optional[str] = None):
        """Delete multiple nodes

        Importance notes:
        1. Changes will be persisted to disk during the next index_done_callback
        2. Only one process should updating the storage at a time before index_done_callback,
           KG-storage-log should be used to avoid data corruption

        Args:
            nodes: List of node IDs to be deleted
            namespace: namespace for data
        """

    @abstractmethod
    async def remove_edges(self, edges: list[tuple[str, str]], namespace: Optional[str] = None):
        """Delete multiple edges

        Importance notes:
        1. Changes will be persisted to disk during the next index_done_callback
        2. Only one process should updating the storage at a time before index_done_callback,
           KG-storage-log should be used to avoid data corruption

        Args:
            edges: List of edges to be deleted, each edge is a (source, target) tuple
            namespace: namespace for data
        """

    @abstractmethod
    async def get_all_labels(self, namespace: Optional[str] = None) -> list[str]:
        """Get all labels in the graph.

        Returns:
            A list of all node labels in the graph, sorted alphabetically
            namespace: namespace for data
        """

    @abstractmethod
    async def get_knowledge_graph(
        self, node_label: str, max_depth: int = 3, max_nodes: int = 1000, namespace: Optional[str] = None
    ) -> KnowledgeGraph:
        """
        Retrieve a connected subgraph of nodes where the label includes the specified `node_label`.

        Args:
            node_label: Label of the starting node，* means all nodes
            max_depth: Maximum depth of the subgraph, Defaults to 3
            max_nodes: Maxiumu nodes to return, Defaults to 1000（BFS if possible)
            namespace: namespace for storage data
        Returns:
            KnowledgeGraph object containing nodes and edges, with an is_truncated flag
            indicating whether the graph was truncated due to max_nodes limit
        """


class DocStatus(str, Enum):
    """Document processing status"""

    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


@dataclass
class DocProcessingStatus:
    """Document processing status data structure"""

    content: str
    """Original content of the document"""
    content_summary: str
    """First 100 chars of document content, used for preview"""
    content_length: int
    """Total length of document"""
    file_path: str
    """File path of the document"""
    status: DocStatus
    """Current processing status"""
    created_at: str
    """ISO format timestamp when document was created"""
    updated_at: str
    """ISO format timestamp when document was last updated"""
    chunks_count: int | None = None
    """Number of chunks after splitting, used for processing"""
    error: str | None = None
    """Error message if failed"""
    metadata: dict[str, Any] = field(default_factory=dict)
    """Additional metadata"""


@dataclass
class DocStatusStorage(BaseKVStorage, ABC):
    """Base class for document status storage"""

    @abstractmethod
    async def get_status_counts(self, workspace: str = "default") -> dict[str, int]:
        """Get counts of documents in each status"""

    @abstractmethod
    async def get_docs_by_status(
        self, status: DocStatus, workspace: str = "default"
    ) -> dict[str, DocProcessingStatus]:
        """Get all documents with a specific status"""

    async def drop_cache_by_modes(self, modes: list[str] | None = None) -> bool:
        """Drop cache is not supported for Doc Status storage"""
        return False


class StoragesStatus(str, Enum):
    """Storages status"""

    NOT_CREATED = "not_created"
    CREATED = "created"
    INITIALIZED = "initialized"
    FINALIZED = "finalized"
