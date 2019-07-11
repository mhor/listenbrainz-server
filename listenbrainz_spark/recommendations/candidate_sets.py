import os
import sys
import uuid
import json
import logging
from time import time
from datetime import datetime
from collections import defaultdict
from py4j.protocol import Py4JJavaError

import listenbrainz_spark
from listenbrainz_spark import config, utils
from listenbrainz_spark.sql import get_user_id
from listenbrainz_spark.exceptions import SQLException
from listenbrainz_spark.recommendations.utils import save_html
from listenbrainz_spark.sql import candidate_sets_queries as sql

from pyspark.sql.utils import AnalysisException, ParseException

# Candidate Set HTML is generated if set to true.
SAVE_CANDIDATE_HTML = True

def get_similar_artists(top_artists_df, user_name):
    """ Get similar artists dataframe.

        Args:
            top_artists_df (dataframe): Dataframe containing top artists of the user.
            user_name (str): User name of the user.

        Returns:
            similar_artists_df (dataframe): Columns can be depicted as:
                [
                    'artist_name', 'similar_artist_name'
                ]
    """
    top_artists = [row.artist_name for row in top_artists_df.collect()]

    if len(top_artists) == 1:
        # Handle tuple with single entity
        similar_artists_df = sql.get_similar_artists_with_limit(tuple(top_artists[0]))
    else:
        similar_artists_df = sql.get_similar_artists_with_limit(tuple(top_artists))

    try:
        similar_artists_df.take(1)[0]
    except IndexError as err:
        logging.error('{}\n{}\nNo similar artists found for top artists listened to by "{}". All the top artists are with' \
            ' zero collaborations therefore top artists and similar artists candidate set cannot be generated' \
            .format(type(err).__name__, str(err), user_name))
        raise
    return similar_artists_df

def get_top_artists_recording_ids(similar_artist_df, user_name, user_id):
    """ Get recording ids of top artists.

        Args:
            similar_artists_df (dataframe): Dataframe consisting of similar artists.
            user_name (str): User name of the user.
            user_id (int): User id of the user.

        Returns:
            top_artists_recordings_ids_df (dataframe): Columns can be depicted as:
                [
                    'user_id', 'recording_id'
                ]
    """
    # top artists with collaborations not equal to zero.
    top_artists_with_collab_df = sql.get_top_artists_with_collab()
    top_artists_with_collab = [row.artist_name for row in top_artists_with_collab_df.collect()]

    if len(top_artists_with_collab) == 1:
        top_artists_recording_ids_df = sql.get_candidate_recording_ids(tuple((
            top_artists_with_collab[0])),user_id)
    else:
        top_artists_recording_ids_df = sql.get_candidate_recording_ids(tuple(top_artists_with_collab), user_id)
    return top_artists_recording_ids_df

def get_similar_artists_recording_ids(similar_artists_df, top_artists_df, user_name, user_id):
    """ Get recording ids of similar artists.

        Args:
            similar_artists_df (dataframe): Dataframe consisting of similar artists.
            top_artists_df (dataframe) : Dataframe consisting of top artists.
            user_name (str): User name of the user.
            user_id (int): User id of the user.

        Returns:
            similar_artists_recording_ids_df (dataframe): Columns can be depicted as:
                [
                    'user_id', 'recording_id'
                ]
    """
    # eliminate artists from similar artists who are a part of top artists
    similar_artists_df = sql.get_net_similar_artists()
    try:
        similar_artists_df.take(1)[0]
    except IndexError as err:
        logging.error('{}\n{}\nSimilar artists candidate set not generated for "{}" as similar artists are' \
            ' equivalent to top artists for the user'.format(type(err).__name__, str(err), user_name))
        raise
    similar_artists = [row.similar_artist_name for row in similar_artists_df.collect()]

    if len(similar_artists) == 1:
        similar_artists_recording_ids_df = sql.get_candidate_recording_ids(tuple(similar_artists[0]), user_id)
    else:
        similar_artists_recording_ids_df = sql.get_candidate_recording_ids(tuple(similar_artists), user_id)

    try:
        similar_artists_recording_ids_df.take(1)[0]
    except IndexError as err:
        logging.error('{}\n{}\nNo recordings found associated to artists in similar artists set. Similar artists' \
            ' candidate set cannot be generated for "{}"'.format(type(err).__name__, str(err), user_name))
        raise
    return similar_artists_recording_ids_df

def save_candidate_sets(top_artists_candidate_set_df, similar_artists_candidate_set_df):
    """ Save candidate sets to HDFS.

        Args:
            top_artists_candidate_set_df (dataframe): Dataframe consisting of recording ids of
                top artists listened to by a user for all the users for whom recommendations shall
                be generated. Dataframe columns can be depicted as:
                    [
                        'user_id', 'recording_id'
                    ]
            similar_artists_candidate_set_df (dataframe): Dataframe consisting of recording ids of
                artists similar to top artists listened to by a user for all the users for whom
                recommendations shall be generated. Columns can be depicted as:
                    [
                        'user_id', 'recording_id'
                    ]
    """
    path = os.path.join(config.HDFS_CLUSTER_URI, 'data', 'listenbrainz', 'recommendation-engine', 'candidate-set')
    utils.save_parquet(top_artists_candidate_set_df, path  + '/top_artists.parquet')
    utils.save_parquet(similar_artists_candidate_set_df, path + '/similar_artists.parquet')

def get_candidate_html_data(similar_artist_df, user_name):
    """ Get artists similar to top artists listened to by the user. The function is invoked
        when candidate set HTML is to be generated.

        Args:
            top_artists_with_collab (dataframe): Dataframe of top artists listened to by the user
                whose similar artists count is not zero.
            user_name (str): User name of the user.

        Returns:
            artists (dict): Dictionary can be depicted as:
                {
                    'top_artists 1' : ['similar_artist 1', 'similar_artist 2' ... 'similar_artist x'],
                    'top_artists 2' : ['similar_artist 1', 'similar_artist 2' ... 'similar_artist x'],
                    .
                    .
                    .
                    'top_artists y' : ['similar_artist 1', 'similar_artist 2' ... 'similar_artist x'],
                }
    """
    artists = defaultdict(dict)
    top_artist_with_collab_df = sql.get_top_artists_with_collab()
    for row in top_artist_with_collab_df.collect():
        df = sql.get_similar_artists_for_candidate_html(row.artist_name)
        artists[row.artist_name] = [row.similar_artist_name for row in df.collect()]
    return artists

def save_candidate_html(user_data):
    """ Save user data to an HTML file.

        Args:
            user_data (dict): Dictionary can be depicted as:
                {
                    'user_name 1': {
                        'artists': {
                            'top_artists 1' : ['similar_artist 1', 'similar_artist 2' ... 'similar_artist x'],
                        ...
                        'top_artists y' : ['similar_artist 1', 'similar_artist 2' ... 'similar_artist x'],
                        },
                        'time' : 'xxx'
                    },
                }
    """
    date = datetime.utcnow().strftime('%Y-%m-%d')
    candidate_html = 'Candidate-{}-{}.html'.format(uuid.uuid4(), date)
    context = {
        'user_data' : user_data
    }
    save_html(candidate_html, context, 'candidate.html')

def main():
    ti = time()
    try:
        listenbrainz_spark.init_spark_session('Candidate_set')
    except AttributeError:
        logging.info('Aborting...')
        sys.exit(-1)

    try:
        listens_df = utils.get_listens()
    except AttributeError:
        sys.exit(-1)

    if not listens_df:
        logging.error('Parquet files containing listening history from {}-{} to {}-{} missing from HDFS' \
            .format(config.STARTING_YEAR, '{:02d}'.format(config.STARTING_MONTH), config.ENDING_YEAR, '{:02d}' \
            .format( config.ENDING_MONTH)))
        sys.exit(-1)

    try:
        # path where artist relation is stored.
        path = os.path.join(config.HDFS_CLUSTER_URI, 'data', 'listenbrainz', 'similar_artists',
            'artist_artist_relations.parquet')
        artists_relation_df = utils.read_files_from_HDFS(path)

        # path where dataframes are stored.
        path = os.path.join(config.HDFS_CLUSTER_URI, 'data', 'listenbrainz', 'recommendation-engine', 'dataframes')
        recordings_df = utils.read_files_from_HDFS(path + '/recordings_df.parquet')
        users_df = utils.read_files_from_HDFS(path + '/users_df.parquet')
    except AnalysisException:
        logging.info('Aborting...')
        sys.exit(-1)
    except AttributeError:
        logging.info('Aborting...')
        sys.exit(-1)

    logging.info('Registering Dataframes...')
    try:
        utils.register_dataframe(listens_df, 'listens_df')
        utils.register_dataframe(recordings_df, 'recording')
        utils.register_dataframe(users_df, 'user')
        utils.register_dataframe(artists_relation_df, 'artists_relation')
    except AnalysisException:
        logging.info('Aborting...')
        sys.exit(-1)
    except AttributeError:
        logging.info('Aborting...')
        sys.exit(-1)
    logging.info('Files fetched from HDFS and dataframes registered in {}s'.format('{:.2f}'.format(time() - ti)))

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),'recommendation-metadata.json')
    with open(path) as f:
        recommendation_metadata = json.load(f)
        user_names = recommendation_metadata['user_name']

    user_data = defaultdict(dict)
    similar_artists_candidate_set_df = None
    top_artists_candidate_set_df = None
    for user_name in user_names:
        ts = time()
        try:
            user_id = get_user_id(user_name)
        except TypeError as err:
            logging.error('{}: Invalid user name. User "{}" does not exist.'.format(type(err).__name__,user_name))
            continue
        except SQLException as err:
            logging.error('User id for "{}" cannot be retrieved: {}\n{}'.format(user_name, type(err).__name__, str(err)))
            continue

        try:
            top_artists_df = sql.get_top_artists(user_name)
            top_artists_df.take(1)[0]
        except IndexError as err:
            logging.error('{}\n{}\nNo top artists found, i.e. "{}" is either a new user or has empty listening history.' \
                ' Candidate sets cannot be generated'.format(type(err).__name__, str(err), user_name))
            continue
        except SQLException as err:
            logging.error('Top artists cannot be retrieved for "{}": {}\n{}'.format(user_name, type(err).__name__, str(err)))
            continue

        try:
            similar_artists_df = get_similar_artists(top_artists_df, user_name)
        except IndexError:
            continue
        except SQLException as err:
            logging.error('Candidate sets not generated for "{}"\n{}'.format(user_name, err))
            continue

        try:
            utils.register_dataframe(similar_artists_df, 'similar_artist')
            utils.register_dataframe(top_artists_df, 'top_artist')
        except AnalysisException:
            continue
        except AttributeError:
            continue

        try:
            top_artists_recording_ids_df = get_top_artists_recording_ids(similar_artists_df, user_name, user_id)
        except SQLException as err:
            logging.error('Candidate sets could not be generated for "{}"\n{}'.format(user_name, err))
            continue
        top_artists_candidate_set_df = top_artists_candidate_set_df.union(top_artists_recording_ids_df) \
            if top_artists_candidate_set_df else top_artists_recording_ids_df

        try:
            similar_artists_recording_ids_df = get_similar_artists_recording_ids(similar_artists_df, top_artists_df,
                user_name, user_id)
        except IndexError:
            continue
        except SQLException as err:
            logging.error('Candidate sets could not be generated for "{}"\n{}'.format(user_name, err))
            continue
        similar_artists_candidate_set_df = similar_artists_candidate_set_df.union(similar_artists_recording_ids_df) \
            if similar_artists_candidate_set_df else similar_artists_recording_ids_df

        if SAVE_CANDIDATE_HTML:
            user_data[user_name]['artists'] = get_candidate_html_data(similar_artists_df, user_name)
            user_data[user_name]['time'] = '{:.2f}'.format(time() - ts)
        logging.info('candidate_set generated for \"{}\"'.format(user_name))

    try:
        save_candidate_sets(top_artists_candidate_set_df, similar_artists_candidate_set_df)
    except Py4JJavaError:
        logging.error('Aborting...')
        sys.exit(-1)

    if SAVE_CANDIDATE_HTML:
        try:
            save_candidate_html(user_data)
        except SQLException as err:
            logging.error('Could not save candidate HTML\n{}'.format(err))
            sys.exit(-1)
