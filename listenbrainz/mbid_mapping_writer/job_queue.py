from  concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from queue import PriorityQueue, Queue, Empty
import threading
import traceback

from flask import current_app
from listenbrainz.mbid_mapping_writer.matcher import lookup_new_listens

MAX_THREADS = 2
MAX_QUEUED_JOBS = MAX_THREADS * 2


class MappingJobQueue(threading.Thread):

    def __init__(self, app):
        threading.Thread.__init__(self)
        self.done = False
        self.app = app
        self.queue = PriorityQueue()
        self.priority = 1

    def add_new_listens(self, listens):
        self.queue.put((self.priority, listens))
        self.priority += 1

    def terminate(self):
        self.done = True
        self.join()

    def run(self):
        self.app.logger.info("start job queue thread")

        try:
            with self.app.app_context():
                with ThreadPoolExecutor(max_workers=MAX_THREADS) as executor:
                    futures = {}
                    while not self.done:

                        completed, uncompleted = wait(futures, return_when=FIRST_COMPLETED)
                        for complete in completed:
                            exc = complete.exception()
                            if exc:
                                self.app.logger.info("job %s failed" % futures[complete])
                                self.app.logger.error("\n".join(traceback.format_exception(None, exc, exc.__traceback__)))
                            else:
                                self.app.logger.info("job %s complete" % futures[complete])
                                self.delivery_tag_queue.put(complete.result())
                            del futures[complete]

                        for i in range(MAX_QUEUED_JOBS - len(uncompleted)):
                            try:
                                job = self.queue.get(False)
                            except Empty:
                                break

                            if job[0] > 0:
                                self.app.logger.info("submit job")
                                futures[executor.submit(lookup_new_listens, self.app, job[1])] = job[0]
                            else:
                                self.app.logger.info("Unsupported job type in MappingJobQueue (MBID Mapping Writer).")
        except Exception as err:
            self.app.logger.info(traceback.format_exc())

        self.app.logger.info("job queue thread finished")
