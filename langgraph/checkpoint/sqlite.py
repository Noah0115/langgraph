import json
import pickle
import sqlite3
import threading
from contextlib import AbstractContextManager, contextmanager
from types import TracebackType
from typing import Any, Iterator, Optional, Tuple

from langchain_core.runnables import RunnableConfig
from typing_extensions import Self

from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    SerializerProtocol,
)
from langgraph.serde.jsonplus import JsonPlusSerializer


class JsonPlusSerializerCompat(JsonPlusSerializer):
    """A serializer that supports loading pickled checkpoints for backwards compatibility.

    This serializer extends the JsonPlusSerializer and adds support for loading pickled
    checkpoints. If the input data starts with b"\x80" and ends with b".", it is treated
    as a pickled checkpoint and loaded using pickle.loads(). Otherwise, the default
    JsonPlusSerializer behavior is used.

    Examples:
        >>> import pickle
        >>> from langgraph.checkpoint.sqlite import JsonPlusSerializerCompat
        >>>
        >>> serializer = JsonPlusSerializerCompat()
        >>> pickled_data = pickle.dumps({"key": "value"})
        >>> loaded_data = serializer.loads(pickled_data)
        >>> print(loaded_data)  # Output: {"key": "value"}
        >>>
        >>> json_data = '{"key": "value"}'.encode("utf-8")
        >>> loaded_data = serializer.loads(json_data)
        >>> print(loaded_data)  # Output: {"key": "value"}
    """

    def loads(self, data: bytes) -> Any:
        if data.startswith(b"\x80") and data.endswith(b"."):
            return pickle.loads(data)
        return super().loads(data)


class SqliteSaver(BaseCheckpointSaver, AbstractContextManager):
    """A checkpoint saver that stores checkpoints in a SQLite database.

    Note:
        This class is meant for lightweight, synchronous use cases
        (demos and small projects) and does not
        scale to multiple threads.
        For a similar sqlite saver with `async` support,
        consider using AsyncSqliteSaver.

    Args:
        conn (sqlite3.Connection): The SQLite database connection.
        serde (Optional[SerializerProtocol]): The serializer to use for serializing and deserializing checkpoints. Defaults to JsonPlusSerializerCompat.

    Examples:

        >>> import sqlite3
        >>> from langgraph.checkpoint.sqlite import SqliteSaver
        >>> from langgraph.graph import StateGraph
        >>>
        >>> builder = StateGraph(int)
        >>> builder.add_node("add_one", lambda x: x + 1)
        >>> builder.set_entry_point("add_one")
        >>> builder.set_finish_point("add_one")
        >>> conn = sqlite3.connect("checkpoints.sqlite")
        >>> memory = SqliteSaver(conn)
        >>> graph = builder.compile(checkpointer=memory)
        >>> config = {"configurable": {"thread_id": "1"}}
        >>> graph.get_state(config)
        >>> result = graph.invoke(3, config)
        >>> graph.get_state(config)
        StateSnapshot(values=4, next=(), config={'configurable': {'thread_id': '1', 'thread_ts': '2024-05-04T06:32:42.235444+00:00'}}, parent_config=None)
    """  # noqa

    serde = JsonPlusSerializerCompat()

    conn: sqlite3.Connection
    is_setup: bool

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        serde: Optional[SerializerProtocol] = None,
    ) -> None:
        super().__init__(serde=serde)
        self.conn = conn
        self.is_setup = False
        self.lock = threading.Lock()

    @classmethod
    def from_conn_string(cls, conn_string: str) -> "SqliteSaver":
        """Create a new SqliteSaver instance from a connection string.

        Args:
            conn_string (str): The SQLite connection string.

        Returns:
            SqliteSaver: A new SqliteSaver instance.

        Examples:

            In memory:

                memory = SqliteSaver.from_conn_string(":memory:")

            To disk:

                memory = SqliteSaver.from_conn_string("checkpoints.sqlite")
        """
        return SqliteSaver(
            conn=sqlite3.connect(
                conn_string,
                # https://ricardoanderegg.com/posts/python-sqlite-thread-safety/
                check_same_thread=False,
            )
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        __exc_type: Optional[type[BaseException]],
        __exc_value: Optional[BaseException],
        __traceback: Optional[TracebackType],
    ) -> Optional[bool]:
        return self.conn.close()

    def setup(self) -> None:
        """Set up the checkpoint database.

        This method creates the necessary tables in the SQLite database if they don't
        already exist. It is called automatically when needed and should not be called
        directly by the user.
        """
        if self.is_setup:
            return

        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                thread_id TEXT NOT NULL,
                thread_ts TEXT NOT NULL,
                parent_ts TEXT,
                checkpoint BLOB,
                metadata BLOB,
                PRIMARY KEY (thread_id, thread_ts)
            );
            """
        )

        self.is_setup = True

    @contextmanager
    def cursor(self, transaction: bool = True) -> Iterator[sqlite3.Cursor]:
        """Get a cursor for the SQLite database.

        This method returns a cursor for the SQLite database. It is used internally
        by the SqliteSaver and should not be called directly by the user.

        Args:
            transaction (bool): Whether to commit the transaction when the cursor is closed. Defaults to True.

        Yields:
            sqlite3.Cursor: A cursor for the SQLite database.
        """
        self.setup()
        cur = self.conn.cursor()
        try:
            yield cur
        finally:
            if transaction:
                self.conn.commit()
            cur.close()

    def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
        """Get a checkpoint tuple from the database.

        This method retrieves a checkpoint tuple from the SQLite database based on the
        provided config. If the config contains a "thread_ts" key, the checkpoint with
        the matching thread ID and timestamp is retrieved. Otherwise, the latest checkpoint
        for the given thread ID is retrieved.

        Args:
            config (RunnableConfig): The config to use for retrieving the checkpoint.

        Returns:
            Optional[CheckpointTuple]: The retrieved checkpoint tuple, or None if no matching checkpoint was found.

        Examples:

            Basic:
            >>> config = {"configurable": {"thread_id": "1"}}
            >>> checkpoint_tuple = memory.get_tuple(config)
            >>> print(checkpoint_tuple)
            CheckpointTuple(...)

            With timestamp:

            >>> config = {
            ...    "configurable": {
            ...        "thread_id": "1",
            ...        "thread_ts": "2024-05-04T06:32:42.235444+00:00",
            ...    }
            ... }
            >>> checkpoint_tuple = memory.get_tuple(config)
            >>> print(checkpoint_tuple)
            CheckpointTuple(...)
        """  # noqa
        with self.cursor(transaction=False) as cur:
            if config["configurable"].get("thread_ts"):
                cur.execute(
                    "SELECT checkpoint, parent_ts, metadata FROM checkpoints WHERE thread_id = ? AND thread_ts = ?",
                    (
                        str(config["configurable"]["thread_id"]),
                        str(config["configurable"]["thread_ts"]),
                    ),
                )
                if value := cur.fetchone():
                    return CheckpointTuple(
                        config,
                        self.serde.loads(value[0]),
                        self.serde.loads(value[2]) if value[2] is not None else {},
                        (
                            {
                                "configurable": {
                                    "thread_id": config["configurable"]["thread_id"],
                                    "thread_ts": value[1],
                                }
                            }
                            if value[1]
                            else None
                        ),
                    )
            else:
                cur.execute(
                    "SELECT thread_id, thread_ts, parent_ts, checkpoint, metadata FROM checkpoints WHERE thread_id = ? ORDER BY thread_ts DESC LIMIT 1",
                    (str(config["configurable"]["thread_id"]),),
                )
                if value := cur.fetchone():
                    return CheckpointTuple(
                        {
                            "configurable": {
                                "thread_id": value[0],
                                "thread_ts": value[1],
                            }
                        },
                        self.serde.loads(value[3]),
                        self.serde.loads(value[4]) if value[4] is not None else {},
                        (
                            {
                                "configurable": {
                                    "thread_id": value[0],
                                    "thread_ts": value[2],
                                }
                            }
                            if value[2]
                            else None
                        ),
                    )

    def list(
        self,
        config: RunnableConfig,
        *,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> Iterator[CheckpointTuple]:
        """List checkpoints from the database.

        This method retrieves a list of checkpoint tuples from the SQLite database based
        on the provided config. The checkpoints are ordered by timestamp in descending order.

        Args:
            config (RunnableConfig): The config to use for listing the checkpoints.
            before (Optional[RunnableConfig]): If provided, only checkpoints before the specified timestamp are returned. Defaults to None.
            limit (Optional[int]): The maximum number of checkpoints to return. Defaults to None.

        Yields:
            Iterator[CheckpointTuple]: An iterator of checkpoint tuples.

        Examples:
            >>> from langgraph.checkpoint.sqlite import SqliteSaver
            >>> memory = SqliteSaver.from_conn_string(":memory:")
            ... # Run a graph, then list the checkpoints
            >>> config = {"configurable": {"thread_id": "1"}}
            >>> checkpoints = list(memory.list(config, limit=2))
            >>> print(checkpoints)
            [CheckpointTuple(...), CheckpointTuple(...)]

            >>> config = {"configurable": {"thread_id": "1"}}
            >>> before = {"configurable": {"thread_ts": "2024-05-04T06:32:42.235444+00:00"}}
            >>> checkpoints = list(memory.list(config, before=before))
            >>> print(checkpoints)
            [CheckpointTuple(...), ...]
        """
        query = (
            "SELECT thread_id, thread_ts, parent_ts, checkpoint, metadata FROM checkpoints WHERE thread_id = ? ORDER BY thread_ts DESC"
            if before is None
            else "SELECT thread_id, thread_ts, parent_ts, checkpoint, metadata FROM checkpoints WHERE thread_id = ? AND thread_ts < ? ORDER BY thread_ts DESC"
        )
        if limit:
            query += f" LIMIT {limit}"
        with self.cursor(transaction=False) as cur:
            cur.execute(
                query,
                (
                    (str(config["configurable"]["thread_id"]),)
                    if before is None
                    else (
                        str(config["configurable"]["thread_id"]),
                        before["configurable"]["thread_ts"],
                    )
                ),
            )
            for thread_id, thread_ts, parent_ts, value, metadata in cur:
                yield CheckpointTuple(
                    {"configurable": {"thread_id": thread_id, "thread_ts": thread_ts}},
                    self.serde.loads(value),
                    self.serde.loads(metadata) if metadata is not None else {},
                    (
                        {
                            "configurable": {
                                "thread_id": thread_id,
                                "thread_ts": parent_ts,
                            }
                        }
                        if parent_ts
                        else None
                    ),
                )

    def search(
        self,
        metadata_filter: CheckpointMetadata,
        *,
        before: Optional[RunnableConfig] = None,
        limit: Optional[int] = None,
    ) -> Iterator[CheckpointTuple]:
        """Search for checkpoints by metadata.

        This method retrieves a list of checkpoint tuples from the SQLite
        database based on the provided metadata filter. The metadata filter does
        not need to contain all keys defined in the CheckpointMetadata class.
        The checkpoints are ordered by timestamp in descending order.

        Args:
            metadata_filter (CheckpointMetadata): The metadata filter to use for searching the checkpoints.
            before (Optional[RunnableConfig]): If provided, only checkpoints before the specified timestamp are returned. Defaults to None.
            limit (Optional[int]): The maximum number of checkpoints to return. Defaults to None.

        Yields:
            Iterator[CheckpointTuple]: An iterator of checkpoint tuples.
        """
        # construct query
        SELECT = "SELECT thread_id, thread_ts, parent_ts, checkpoint, metadata FROM checkpoints "
        WHERE, params = search_where(metadata_filter, before)
        ORDER_BY = "ORDER BY thread_ts DESC "
        LIMIT = f"LIMIT {limit}" if limit else ""

        query = f"{SELECT}{WHERE}{ORDER_BY}{LIMIT}"

        # execute query
        with self.cursor(transaction=False) as cur:
            cur.execute(query, params)

            for thread_id, thread_ts, parent_ts, value, metadata in cur:
                yield CheckpointTuple(
                    {"configurable": {"thread_id": thread_id, "thread_ts": thread_ts}},
                    self.serde.loads(value),
                    self.serde.loads(metadata) if metadata is not None else {},
                    (
                        {
                            "configurable": {
                                "thread_id": thread_id,
                                "thread_ts": parent_ts,
                            }
                        }
                        if parent_ts
                        else None
                    ),
                )

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
    ) -> RunnableConfig:
        """Save a checkpoint to the database.

        This method saves a checkpoint to the SQLite database. The checkpoint is associated
        with the provided config and its parent config (if any).

        Args:
            config (RunnableConfig): The config to associate with the checkpoint.
            checkpoint (Checkpoint): The checkpoint to save.
            metadata (Optional[dict[str, Any]]): Additional metadata to save with the checkpoint. Defaults to None.

        Returns:
            RunnableConfig: The updated config containing the saved checkpoint's timestamp.

        Examples:

            >>> from langgraph.checkpoint.sqlite import SqliteSaver
            >>> memory = SqliteSaver.from_conn_string(":memory:")
            ... # Run a graph, then list the checkpoints
            >>> config = {"configurable": {"thread_id": "1"}}
            >>> checkpoint = {"ts": "2024-05-04T06:32:42.235444+00:00", "data": {"key": "value"}}
            >>> saved_config = memory.put(config, checkpoint, {"source": "input", "step": 1, "writes": {"key": "value"}})
            >>> print(saved_config)
            {"configurable": {"thread_id": "1", "thread_ts": 2024-05-04T06:32:42.235444+00:00"}}
        """
        with self.lock, self.cursor() as cur:
            cur.execute(
                "INSERT OR REPLACE INTO checkpoints (thread_id, thread_ts, parent_ts, checkpoint, metadata) VALUES (?, ?, ?, ?, ?)",
                (
                    str(config["configurable"]["thread_id"]),
                    checkpoint["id"],
                    config["configurable"].get("thread_ts"),
                    self.serde.dumps(checkpoint),
                    self.serde.dumps(metadata),
                ),
            )
        return {
            "configurable": {
                "thread_id": config["configurable"]["thread_id"],
                "thread_ts": checkpoint["id"],
            }
        }


def search_where(
    metadata_filter: CheckpointMetadata,
    before: Optional[RunnableConfig] = None,
) -> Tuple[str, Tuple[Any, ...]]:
    """Return WHERE clause predicates for (a)search() given metadata filter
    and `before` config.

    This method returns a tuple of a string and a tuple of values. The string
    is the parametered WHERE clause predicate (including the WHERE keyword):
    "WHERE column1 = ? AND column2 IS ?". The tuple of values contains the
    values for each of the corresponding parameters.
    """
    where = "WHERE "
    param_values = ()

    # construct predicate for metadata filter
    metadata_predicate, metadata_values = _metadata_predicate(metadata_filter)
    if metadata_predicate != "":
        where += metadata_predicate
        param_values += metadata_values

    # construct predicate for `before`
    if before is not None:
        if metadata_predicate != "":
            where += "AND thread_ts < ? "
        else:
            where += "thread_ts < ? "

        param_values += (before["configurable"]["thread_ts"],)

    if where == "WHERE ":
        # no predicates, return an empty WHERE clause string
        return ("", ())
    else:
        return (where, param_values)


def _metadata_predicate(
    metadata_filter: CheckpointMetadata,
) -> Tuple[str, Tuple[Any, ...]]:
    """Return WHERE clause predicates for (a)search() given metadata filter.

    This method returns a tuple of a string and a tuple of values. The string
    is the parametered WHERE clause predicate (excluding the WHERE keyword):
    "column1 = ? AND column2 IS ?". The tuple of values contains the values
    for each of the corresponding parameters.
    """

    def _where_value(query_value: Any) -> Tuple[str, Any]:
        """Return tuple of operator and value for WHERE clause predicate."""
        if query_value is None:
            return ("IS ?", None)
        elif (
            isinstance(query_value, str)
            or isinstance(query_value, int)
            or isinstance(query_value, float)
        ):
            return ("= ?", query_value)
        elif isinstance(query_value, bool):
            return ("= ?", 1 if query_value else 0)
        elif isinstance(query_value, dict) or isinstance(query_value, list):
            # query value for JSON object cannot have trailing space after separators (, :)
            # SQLite json_extract() returns JSON string without whitespace
            return ("= ?", json.dumps(query_value, separators=(",", ":")))
        else:
            return ("= ?", str(query_value))

    predicate = ""
    param_values = ()

    # process metadata query
    for query_key, query_value in metadata_filter.items():
        operator, param_value = _where_value(query_value)
        predicate += (
            f"json_extract(CAST(metadata AS TEXT), '$.{query_key}') {operator} AND "
        )
        param_values += (param_value,)

    if predicate != "":
        # remove trailing AND
        predicate = predicate[:-4]

    # predicate contains an extra trailing space
    return (predicate, param_values)
