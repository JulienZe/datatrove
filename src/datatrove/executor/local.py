from copy import deepcopy
from functools import partial
from typing import Callable

import multiprocess
from loguru import logger

from datatrove.executor.base import PipelineExecutor
from datatrove.io import DataFolderLike
from datatrove.pipeline.base import PipelineStep
from datatrove.utils.stats import PipelineStats


class LocalPipelineExecutor(PipelineExecutor):
    def __init__(
        self,
        pipeline: list[PipelineStep | Callable],
        tasks: int = 1,
        workers: int = -1,
        logging_dir: DataFolderLike = None,
        skip_completed: bool = True,
        start_method: str = "forkserver",
        local_tasks: int = -1,
        local_rank_offset: int = 0,
    ):
        """Execute a pipeline locally

        Args:
            pipeline: a list of PipelineStep and/or custom functions
                with arguments (data: DocumentsPipeline, rank: int,
                world_size: int)
            tasks: total number of tasks to run the pipeline on
            workers: how many tasks to run simultaneously. -1 for no
                limit
            logging_dir: where to save logs, stats, etc. Should be parsable into a datatrove.io.DataFolder
            skip_completed: whether to skip tasks that were completed in
                previous runs. default: True
            start_method: method to use to spawn a multiprocessing Pool
            local_tasks: how many of the total tasks should be run on this node/machine. -1 for all
            local_rank_offset: the rank of the first task to run on this machine.
                Tasks [local_rank_offset, local_rank_offset + local_tasks] will be run.
        """
        super().__init__(pipeline, logging_dir, skip_completed)
        self.tasks = tasks
        self.workers = workers if workers != -1 else tasks
        self.start_method = start_method
        self.local_tasks = local_tasks if local_tasks != -1 else tasks
        self.local_rank_offset = local_rank_offset
        if self.local_rank_offset + self.local_tasks > self.tasks:
            raise ValueError(
                f"Local tasks go beyond the total tasks (local_rank_offset + local_tasks = {self.local_rank_offset + self.local_tasks} > {self.tasks} = tasks)"
            )

    def _launch_run_for_rank(self, rank: int, ranks_q, completed=None, completed_lock=None) -> PipelineStats:
        """
            Small wrapper around _run_for_rank with a queue of available local ranks.
        Args:
            rank: rank to run pipeline for
            ranks_q: queue of local ranks
            completed: counter with the number of complete tasks
            completed_lock: lock to synchronize completed counter

        Returns: the stats for this task

        """
        local_rank = ranks_q.get()
        try:
            return self._run_for_rank(rank, local_rank)
        finally:
            if completed and completed_lock:
                with completed_lock:
                    completed.value += 1
                    logger.info(f"{completed.value}/{self.world_size} tasks completed.")
            ranks_q.put(local_rank)  # free up used rank

    def run(self):
        """
            This method is responsible for correctly invoking `self._run_for_rank` for each task that is to be run.

            On a LocalPipelineExecutor, this method will spawn a multiprocess pool if workers != 1.
            Otherwise, ranks will be run sequentially in a loop.
        Returns:

        """
        if all(map(self.is_rank_completed, range(self.local_rank_offset, self.local_rank_offset + self.local_tasks))):
            logger.info(f"Not doing anything as all {self.local_tasks} tasks have already been completed.")
            return

        self.save_executor_as_json()
        mg = multiprocess.Manager()
        ranks_q = mg.Queue()
        for i in range(self.workers):
            ranks_q.put(i)

        ranks_to_run = self.get_incomplete_ranks(
            range(self.local_rank_offset, self.local_rank_offset + self.local_tasks)
        )
        if (skipped := self.local_tasks - len(ranks_to_run)) > 0:
            logger.info(f"Skipping {skipped} already completed tasks")

        if self.workers == 1:
            pipeline = self.pipeline
            stats = []
            for rank in ranks_to_run:
                self.pipeline = deepcopy(pipeline)
                stats.append(self._launch_run_for_rank(rank, ranks_q))
        else:
            completed_counter = mg.Value("i", skipped)
            completed_lock = mg.Lock()
            ctx = multiprocess.get_context(self.start_method)
            with ctx.Pool(self.workers) as pool:
                stats = list(
                    pool.imap_unordered(
                        partial(
                            self._launch_run_for_rank,
                            ranks_q=ranks_q,
                            completed=completed_counter,
                            completed_lock=completed_lock,
                        ),
                        ranks_to_run,
                    )
                )
        # merged stats
        stats = sum(stats, start=PipelineStats())
        with self.logging_dir.open("stats.json", "wt") as statsfile:
            stats.save_to_disk(statsfile)
        logger.success(stats.get_repr(f"All {self.local_tasks} tasks"))
        return stats

    @property
    def world_size(self) -> int:
        """
            Simply the number of tasks
        Returns:

        """
        return self.tasks
