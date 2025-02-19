#  Copyright 2023 LanceDB Developers
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
from __future__ import annotations

import asyncio
from typing import Awaitable, Literal

import numpy as np
import pandas as pd
import pyarrow as pa

from .common import VECTOR_COLUMN_NAME


class LanceQueryBuilder:
    """
    A builder for nearest neighbor queries for LanceDB.

    Examples
    --------
    >>> import lancedb
    >>> data = [{"vector": [1.1, 1.2], "b": 2},
    ...         {"vector": [0.5, 1.3], "b": 4},
    ...         {"vector": [0.4, 0.4], "b": 6},
    ...         {"vector": [0.4, 0.4], "b": 10}]
    >>> db = lancedb.connect("./.lancedb")
    >>> table = db.create_table("my_table", data=data)
    >>> (table.search([0.4, 0.4])
    ...       .metric("cosine")
    ...       .where("b < 10")
    ...       .select(["b"])
    ...       .limit(2)
    ...       .to_df())
       b      vector  score
    0  6  [0.4, 0.4]    0.0
    """

    def __init__(
        self,
        table: "lancedb.table.LanceTable",
        query: np.ndarray,
        vector_column_name: str = VECTOR_COLUMN_NAME,
    ):
        self._metric = "L2"
        self._nprobes = 20
        self._refine_factor = None
        self._table = table
        self._query = query
        self._limit = 10
        self._columns = None
        self._where = None
        self._vector_column_name = vector_column_name

    def limit(self, limit: int) -> LanceQueryBuilder:
        """Set the maximum number of results to return.

        Parameters
        ----------
        limit: int
            The maximum number of results to return.

        Returns
        -------
        LanceQueryBuilder
            The LanceQueryBuilder object.
        """
        self._limit = limit
        return self

    def select(self, columns: list) -> LanceQueryBuilder:
        """Set the columns to return.

        Parameters
        ----------
        columns: list
            The columns to return.

        Returns
        -------
        LanceQueryBuilder
            The LanceQueryBuilder object.
        """
        self._columns = columns
        return self

    def where(self, where: str) -> LanceQueryBuilder:
        """Set the where clause.

        Parameters
        ----------
        where: str
            The where clause.

        Returns
        -------
        LanceQueryBuilder
            The LanceQueryBuilder object.
        """
        self._where = where
        return self

    def metric(self, metric: Literal["L2", "cosine"]) -> LanceQueryBuilder:
        """Set the distance metric to use.

        Parameters
        ----------
        metric: "L2" or "cosine"
            The distance metric to use. By default "L2" is used.

        Returns
        -------
        LanceQueryBuilder
            The LanceQueryBuilder object.
        """
        self._metric = metric
        return self

    def nprobes(self, nprobes: int) -> LanceQueryBuilder:
        """Set the number of probes to use.

        Higher values will yield better recall (more likely to find vectors if
        they exist) at the expense of latency.

        See discussion in [Querying an ANN Index][../querying-an-ann-index] for
        tuning advice.

        Parameters
        ----------
        nprobes: int
            The number of probes to use.

        Returns
        -------
        LanceQueryBuilder
            The LanceQueryBuilder object.
        """
        self._nprobes = nprobes
        return self

    def refine_factor(self, refine_factor: int) -> LanceQueryBuilder:
        """Set the refine factor to use, increasing the number of vectors sampled.

        As an example, a refine factor of 2 will sample 2x as many vectors as
        requested, re-ranks them, and returns the top half most relevant results.

        See discussion in [Querying an ANN Index][querying-an-ann-index] for
        tuning advice.

        Parameters
        ----------
        refine_factor: int
            The refine factor to use.

        Returns
        -------
        LanceQueryBuilder
            The LanceQueryBuilder object.
        """
        self._refine_factor = refine_factor
        return self

    def to_df(self) -> pd.DataFrame:
        """
        Execute the query and return the results as a pandas DataFrame.
        In addition to the selected columns, LanceDB also returns a vector
        and also the "score" column which is the distance between the query
        vector and the returned vector.
        """

        return self.to_arrow().to_pandas()

    def to_arrow(self) -> pa.Table:
        """
        Execute the query and return the results as a arrow Table.
        In addition to the selected columns, LanceDB also returns a vector
        and also the "score" column which is the distance between the query
        vector and the returned vector.
        """
        if self._table._conn.is_managed_remote:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.get_event_loop()
            result = self._table._conn._client.query(
                self._table.name, self.to_remote_query()
            )
            return loop.run_until_complete(result).to_arrow()

        ds = self._table.to_lance()
        return ds.to_table(
            columns=self._columns,
            filter=self._where,
            nearest={
                "column": self._vector_column_name,
                "q": self._query,
                "k": self._limit,
                "metric": self._metric,
                "nprobes": self._nprobes,
                "refine_factor": self._refine_factor,
            },
        )

    def to_remote_query(self) -> "VectorQuery":
        # don't import unless we are connecting to remote
        from lancedb.remote.client import VectorQuery

        return VectorQuery(
            vector=self._query.tolist(),
            filter=self._where,
            k=self._limit,
            _metric=self._metric,
            columns=self._columns,
            nprobes=self._nprobes,
            refine_factor=self._refine_factor,
        )


class LanceFtsQueryBuilder(LanceQueryBuilder):
    def to_df(self) -> pd.DataFrame:
        try:
            import tantivy
        except ImportError:
            raise ImportError(
                "Please install tantivy-py `pip install tantivy@git+https://github.com/quickwit-oss/tantivy-py#164adc87e1a033117001cf70e38c82a53014d985` to use the full text search feature."
            )

        from .fts import search_index

        # get the index path
        index_path = self._table._get_fts_index_path()
        # open the index
        index = tantivy.Index.open(index_path)
        # get the scores and doc ids
        row_ids, scores = search_index(index, self._query, self._limit)
        if len(row_ids) == 0:
            return pd.DataFrame()
        scores = pa.array(scores)
        output_tbl = self._table.to_lance().take(row_ids, columns=self._columns)
        output_tbl = output_tbl.append_column("score", scores)
        return output_tbl.to_pandas()
