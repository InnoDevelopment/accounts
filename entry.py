import logging
import re

from bson import ObjectId
from flask import Flask, send_from_directory
from flask_restful import reqparse, Api, Resource, request
from pymongo import MongoClient
from werkzeug.security import generate_password_hash, \
    check_password_hash

import config
import utils
import utils.common
from utils.validators import is_password_valid
from utils.validators import is_username_valid

# constants

API_PREFIX = '/api/v%s/%s' % (config.VERSION, config.APP_NAME)

# variables

app = Flask(config.APP_NAME)
api = Api(app, API_PREFIX)

db_client = MongoClient(host=config.DB_HOST, port=config.DB_PORT)
accounts_db = db_client.accounts

accounts_collection = accounts_db.accounts


# helper functions


def get_account_by_username(username):
    username_regex = re.compile(('^%s$' % username), re.IGNORECASE)
    return accounts_collection.find_one({'username': {'$regex': username_regex}})


def get_account_by_token(token):
    return accounts_collection.find_one({'token': token})


def get_account_by_id(id):
    return accounts_collection.find_one({'_id': ObjectId(id)})


def RESULT(status, result, code, error):
    out = {'status': status}

    if result is not None:
        out['result'] = result

    if error is not None:
        out['error'] = error

    return out, code


def RESULT_OK(result=None, code=200):
    return RESULT('ok', result, code, None)


def RESULT_OK_CREATED(result=None, code=201):
    return RESULT('ok', result, code, None)


def RESULT_FAIL_ON_CLIENT(error, status='fail', code=400):
    return RESULT(status, None, code, error)


def setup_logger():
    log_format = '[%(asctime)s] PID %(process)s (%(pathname)s:%(lineno)d) %(levelname)s: %(message)s'

    logging.basicConfig(format=log_format, filename=config.LOG_GLOBAL, level=logging.DEBUG)

    formatter = logging.Formatter(log_format, '%y-%m-%d %H:%M:%S')

    file_handler = logging.FileHandler(config.LOG_SUMMARY)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.DEBUG)
    stream_handler.setFormatter(formatter)

    app.logger.addHandler(file_handler)
    app.logger.addHandler(stream_handler)

    app.logger.info("Logging has been started.")


def get_basic_request_params(request):
    return {'request_method': request.method,
            'request_url': request.url,
            'remote_addr': request.remote_addr}


def format_log_params(**kwargs):
    log_line = ''
    log_line_template = '%s: %s'
    kw_items = kwargs.items()

    for i, item in enumerate(kw_items):
        if i == 0:
            log_line =  '%s' % (log_line_template % (item[0], item[1]))
        else:
            log_line = '%s, %s' % (log_line, log_line_template % (item[0], item[1]))

    return log_line


# routing


class Account(object):
    def generate_token(self):
        return utils.common.random_string(32)

    def __init__(self, username, password,
                 tgId=None, role='ghost', studyGroup=None,
                 firstName=None, lastName=None, patronymic=None):
        self.token = self.generate_token()
        self.tgId = tgId
        self.role = role
        self.studyGroup = studyGroup
        self.firstName = firstName
        self.lastName = lastName
        self.patronymic = patronymic
        self.username = username
        self.password = password



class AccountsBasic(Resource):
    '''
    getAccount
    '''
    def get(self, token):
        app.logger.info(format_log_params(**get_basic_request_params(request),
                                          method='getAccount',
                                          token=token))
        exclude_fields = ['token', 'password']

        matching_acc = get_account_by_token(token)

        if matching_acc is None:
            return RESULT_FAIL_ON_CLIENT('Unknown token')

        return RESULT_OK(result=utils.common.filter_dict_fields(matching_acc, exclude_fields))


    '''
    createAccount
    '''
    def post(self, token=None):
        app.logger.info(format_log_params(**get_basic_request_params(request),
                                          method='createAccount',
                                          token=token))
        parser = reqparse.RequestParser()

        parser.add_argument('username', type=str)
        parser.add_argument('password', type=str)

        args = parser.parse_args()
        args_names = args.keys()

        if 'username' not in args_names or 'password' not in args_names:
            return RESULT_FAIL_ON_CLIENT('Missing username or password parameters')

        username = args['username']
        password = args['password']

        if username is None or password is None:
            return RESULT_FAIL_ON_CLIENT('Missing username or password parameters')

        if (not is_username_valid(username)) or (not is_password_valid(password)):
            return RESULT_FAIL_ON_CLIENT('Incorrect username or password formation: illegal length or content')

        # check if username is taken
        acc = get_account_by_username(username)

        if acc is not None:
            # it is, so try choosing another one
            return RESULT_FAIL_ON_CLIENT('Username is already taken')

        # create new account

        hashed_password = generate_password_hash(password)

        acc_hash = Account(username, hashed_password).__dict__

        accounts_collection.insert_one(acc_hash)

        return RESULT_OK_CREATED(
            result=utils.common.filter_dict_fields(
                acc_hash, None,
                ['id', 'username', 'role',
                 'firstName', 'lastName', 'token']))


class AccountsAuthorizedActions(Resource):
    def exists(self, token):
        app.logger.info(format_log_params(**get_basic_request_params(request),
                                          method='exists',
                                          token=token))
        acc = accounts_collection.find_one({'token': token})

        return RESULT_OK(result=acc is not None)

    def updateRole(self, token):
        moderator_account = get_account_by_token(token)

        if moderator_account is None:
            return RESULT_FAIL_ON_CLIENT('Unknown token')

        if moderator_account['role'] != 'moderator':
            return RESULT_FAIL_ON_CLIENT('Unexpected account role. Needed: moderator')

        parser = reqparse.RequestParser()

        parser.add_argument('accountId', type=str)
        parser.add_argument('newRole', type=str)

        args = parser.parse_args()
        args_names = args.keys()

        if 'accountId' not in args_names or 'newRole' not in args_names:
            return RESULT_FAIL_ON_CLIENT('No accountID specified')

        editable_user_account_id = args['accountId']
        new_role = args['newRole']

        # TODO: advanced permission management

        if (editable_user_account_id is None) or (new_role is None) or new_role not in ['student', 'ghost']:
            return RESULT_FAIL_ON_CLIENT('accountID provided is not valid(probably, roles do not match)')

        editable_action_account = get_account_by_id(editable_user_account_id)
        editable_action_account['role'] = new_role

        accounts_collection.update_one({'_id': ObjectId(editable_action_account['_id'])},
                                       {'$set': {'role': new_role}})

        return RESULT_OK()

    def listAccounts(self, token):
        account = get_account_by_token(token)

        if account is None or account['role'] != 'moderator':
            return RESULT_FAIL_ON_CLIENT('Unknown token')

        found_accounts = list(accounts_collection\
            .find({'role': {'$in': ['student', 'ghost']}})\
            .sort('role', 1))

        found_accounts_slices = []

        for acc in found_accounts:
            found_accounts_slices\
                .append(utils.common.filter_dict_fields(acc,
                                                        None,
                                                        ['id', 'username', 'role', 'firstName', 'lastName']))

        return RESULT_OK(result=found_accounts_slices)

    def process_request(self, method, token, action):
        app.logger.info(format_log_params(**get_basic_request_params(request),
                                          method=action,
                                          token=token))
        handlers = {'get': {'exists': self.exists,
                            'listAccounts': self.listAccounts},
                    'put': {'updateRole': self.updateRole}}

        if action in dir(self):
            if method not in handlers.keys() or action not in handlers[method]:
                return RESULT_FAIL_ON_CLIENT('No action handler provided for action: %s' % action)

            func = handlers[method][action]

            if utils.common.is_function(func):
                return func(token)
            else:
                return RESULT_FAIL_ON_CLIENT('No action handler provided for action: %s' % action)
        else:
            return RESULT_FAIL_ON_CLIENT('No action handler provided for action: %s' % action)

    def get(self, token, action):
        return self.process_request('get', token, action)

    def post(self, token, action):
        return self.process_request('post', token, action)

    def put(self, token, action):
        return self.process_request('put', token, action)


class AccountsUnauthorizedActions(Resource):
    def auth(self):
        parser = reqparse.RequestParser()

        parser.add_argument('username', type=str)
        parser.add_argument('password', type=str)

        args = parser.parse_args()
        args_names = args.keys()

        if 'username' not in args_names or 'password' not in args_names:
            return RESULT_FAIL_ON_CLIENT('Missing username or password parameters')

        username = args['username']
        password = args['password']

        # create new account
        if username is None or password is None:
            return RESULT_FAIL_ON_CLIENT('Missing username or password parameters')

        account = get_account_by_username(username)

        if account is None:
            return RESULT_FAIL_ON_CLIENT('Unknown username or password')

        acc_password = account['password']

        authorized = check_password_hash(acc_password, password)

        if authorized:
            return RESULT_OK(
                result=utils.common.filter_dict_fields(account,
                                                       None,
                                                       ['id', 'username', 'role',
                                                   'firstName', 'lastName', 'token']))
        else:
            return RESULT_FAIL_ON_CLIENT('Unknown username or password')

    def post(self, action):
        app.logger.info(format_log_params(**get_basic_request_params(request),
                                          method=action))
        if action in dir(self):
            func = getattr(self, action)

            if utils.common.is_function(func):
                return func()
            else:
                return RESULT_FAIL_ON_CLIENT('No action handler provided for action: %s' % action)
        else:
            return RESULT_FAIL_ON_CLIENT('No action handler provided for action: %s' % action)


api.add_resource(AccountsUnauthorizedActions, '/<string:action>')
api.add_resource(AccountsBasic, '/<string:token>', '/')
api.add_resource(AccountsAuthorizedActions, '/<string:token>/<string:action>')


if __name__ == '__main__':
    setup_logger()

    app.run(config.WEB_HOST, config.WEB_PORT, debug=True)

