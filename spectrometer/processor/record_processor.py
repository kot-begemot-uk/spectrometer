# Copyright (c) 2013 Mirantis Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import bisect
import collections
import copy
import time

import six

from spectrometer.openstack.common import log as logging
from spectrometer.processor import utils


LOG = logging.getLogger(__name__)


class RecordProcessor(object):
    def __init__(self, runtime_storage_inst):
        self.runtime_storage_inst = runtime_storage_inst

        self.domains_index = runtime_storage_inst.get_by_key('companies')

        self.releases = runtime_storage_inst.get_by_key('releases')
        self.releases_dates = [r['end_date'] for r in self.releases]

        self.modules = None
        self.alias_module_map = None

    def _get_release(self, timestamp):
        release_index = bisect.bisect(self.releases_dates, timestamp)
        if release_index >= len(self.releases):
            LOG.warn('Timestamp %s is beyond releases boundaries, the last '
                     'release will be used. Please consider adding a '
                     'new release into default_data.json', timestamp)
            release_index = len(self.releases) - 1
        return self.releases[release_index]['release_name']

    def _get_modules(self):
        if self.modules is None:
            self.modules = set()
            self.alias_module_map = dict()

            for repo in utils.load_repos(self.runtime_storage_inst):
                module = repo['module'].lower()
                module_aliases = repo.get('aliases') or []

                add = True
                for module_name in ([module] + module_aliases):
                    for m in self.modules:
                        if module_name.find(m) >= 0:
                            add = False
                            break
                        if m.find(module_name) >= 0:
                            self.modules.remove(m)
                            break
                    if add:
                        self.modules.add(module_name)

                for alias in module_aliases:
                    self.alias_module_map[alias] = module

        return self.modules, self.alias_module_map

    def _find_company(self, companies, date):
        for r in companies:
            if date < r['end_date']:
                return r['company_name']
        return companies[-1]['company_name']

    def _get_company_by_email(self, email):
        if not email:
            return None

        name, at, domain = email.partition('@')
        if domain:
            parts = domain.split('.')
            for i in range(len(parts), 1, -1):
                m = '.'.join(parts[len(parts) - i:])
                if m in self.domains_index:
                    return self.domains_index[m]
        return None

    def _create_user(self, ldap_id, email, user_name):
        company = (self._get_company_by_email(email) or
                   self._get_independent())
        user = {
            'user_id': ldap_id or email,
            'ldap_id': ldap_id,
            'user_name': user_name or '',
            'companies': [{
                'company_name': company,
                'end_date': 0,
            }],
        }
        if email:
            user['emails'] = [email]
        else:
            user['emails'] = []
        return user

    def _get_independent(self):
        return '*independent'

    def _update_user_affiliation(self, user):
        for email in user.get('emails'):
            company_name = self._get_company_by_email(email)
            uc = user['companies']
            if (company_name and (len(uc) == 1) and
                    (uc[0]['company_name'] == self._get_independent())):
                LOG.debug('Updating affiliation of user %s to %s',
                          user['user_id'], company_name)
                uc[0]['company_name'] = company_name
                break

    def _get_user_exact_company(self, user):
        if len(user.get('companies', [])) == 1:
            return user['companies'][0]['company_name']
        return None

    def _merge_user_profiles(self, user_a, user_b, user_c):
        user = {}
        for key in ['seq', 'user_name', 'user_id',
                    'ldap_id', 'companies']:
            user[key] = user_a.get(key) or user_b.get(key) or user_c.get(key)

        if user['ldap_id'] and user['user_id'] != user['ldap_id']:
            user['user_id'] = user['ldap_id']

        emails = set([])
        core_in = set([])
        for u in [user_a, user_b, user_c]:
            emails |= set(u.get('emails', []))
            core_in |= set(u.get('core', []))
        user['emails'] = list(emails)
        user['core'] = list(core_in)

        self._update_user_affiliation(user)

        if user_a.get('seq') and user_b.get('seq'):
            LOG.debug('Delete user: %s', user_b)
            utils.delete_user(self.runtime_storage_inst, user_b)
        return user

    def update_user(self, record):
        email = record.get('author_email')
        user_e = utils.load_user(self.runtime_storage_inst, email) or {}

        user_name = record.get('author_name')

        ldap_id = record.get('ldap_id')
        user_l = utils.load_user(self.runtime_storage_inst, ldap_id) or {}

        user = self._create_user(ldap_id, email, user_name)

        if (user_e.get('seq') == user_l.get('seq')) and user_e.get('seq'):
            # sequence numbers are set and the same, merge is not needed
            user = user_e
        else:
            if user_e or user_l:
                user = self._merge_user_profiles(user_e, user_l, user)
            else:
                # Create New
                LOG.debug('Created new user: %s', user)

            utils.store_user(self.runtime_storage_inst, user)

        return user

    def _update_record_and_user(self, record):
        user = self.update_user(record)

        record['user_id'] = user['user_id']
        record['ldap_id'] = user['ldap_id']

        if user.get('user_name'):
            record['author_name'] = user['user_name']

        company = self._find_company(user['companies'], record['date'])
        if company != '*robots':
            company = (self._get_company_by_email(record.get('author_email'))
                       or company)
        record['company_name'] = company

    def _process_commit(self, record):
        record['primary_key'] = record['commit_id']
        record['loc'] = record['lines_added'] + record['lines_deleted']
        record['author_email'] = record['author_email'].lower()
        record['commit_date'] = record['date']

        coauthors = record.get('coauthor')
        if not coauthors:
            self._update_record_and_user(record)

            if record['company_name'] != '*robots':
                yield record
        else:
            coauthors.append({'author_name': record['author_name'],
                              'author_email': record['author_email']})
            for coauthor in coauthors:
                coauthor['date'] = record['date']
                self._update_record_and_user(coauthor)

            for coauthor in coauthors:
                new_record = copy.deepcopy(record)
                new_record.update(coauthor)
                new_record['primary_key'] += coauthor['author_email']

                yield new_record

    def _make_review_record(self, record):
        # copy everything except patchsets and flatten user data
        review = dict([(k, v) for k, v in six.iteritems(record)
                       if k not in ['patchSets', 'owner', 'createdOn']])
        owner = record['owner']

        review['primary_key'] = review['id']
        review['ldap_id'] = owner['username']
        review['author_name'] = owner['name']
        review['author_email'] = owner['email'].lower()
        review['date'] = record['createdOn']

        patch_sets = record.get('patchSets', [])
        review['updated_on'] = review['date']
        if patch_sets:
            patch = patch_sets[-1]
            if 'approvals' in patch:
                review['value'] = min([int(p['value'])
                                       for p in patch['approvals']])
                review['updated_on'] = patch['approvals'][0]['grantedOn']
            else:
                review['updated_on'] = patch['createdOn']

        if 'value' not in review:
            review['value'] = 0

        self._update_record_and_user(review)
        return review

    def _make_patch_record(self, review, patch):
        patch_record = dict()
        patch_record['record_type'] = 'patch'
        patch_record['primary_key'] = utils.get_patch_id(
            review['id'], patch['number'])
        patch_record['number'] = patch['number']
        patch_record['date'] = patch['createdOn']
        uploader = patch['uploader']
        patch_record['ldap_id'] = uploader['username']
        patch_record['author_name'] = uploader['name']
        patch_record['author_email'] = uploader['email'].lower()
        patch_record['module'] = review['module']
        patch_record['branch'] = review['branch']
        patch_record['review_id'] = review['id']

        self._update_record_and_user(patch_record)
        return patch_record

    def _make_mark_record(self, review, patch, approval):
        # copy everything and flatten user data
        mark = dict([(k, v) for k, v in six.iteritems(approval)
                     if k not in ['by', 'grantedOn', 'value', 'description']])
        reviewer = approval['by']

        mark['record_type'] = 'mark'
        mark['value'] = int(approval['value'])
        mark['date'] = approval['grantedOn']
        mark['primary_key'] = (review['id'] + str(mark['date']) + mark['type'])
        mark['ldap_id'] = reviewer['username']
        mark['author_name'] = reviewer['name']
        mark['author_email'] = reviewer['email'].lower()
        mark['module'] = review['module']
        mark['branch'] = review['branch']
        mark['review_id'] = review['id']
        mark['patch'] = int(patch['number'])

        self._update_record_and_user(mark)
        return mark

    def _process_review(self, record):
        """
         Process a review. Review spawns into records of three types:
          * review - records that a user created review request
          * patch - records that a user submitted another patch set
          * mark - records that a user set approval mark to given review
        """
        owner = record['owner']
        if 'email' not in owner or 'username' not in owner:
            return  # ignore

        yield self._make_review_record(record)

        for patch in record.get('patchSets', []):
            if (('email' not in patch['uploader']) or
                    ('username' not in patch['uploader'])):
                continue  # ignore

            yield self._make_patch_record(record, patch)

            if 'approvals' not in patch:
                continue  # not reviewed by anyone

            for approval in patch['approvals']:
                if approval['type'] not in ('Code-Review', 'Workflow'):
                    continue  # keep only Code-Review and Workflow
                if ('email' not in approval['by'] or
                        'username' not in approval['by']):
                    continue  # ignore

                yield self._make_mark_record(record, patch, approval)

    def _guess_module(self, record):
        subject = record['subject'].lower()
        pos = len(subject)
        best_guess_module = None

        modules, alias_module_map = self._get_modules()
        for module in modules:
            find = subject.find(module)
            if (find >= 0) and (find < pos):
                pos = find
                best_guess_module = module

        if best_guess_module:
            if (((pos > 0) and (subject[pos - 1] == '[')) or
                    (not record.get('module'))):
                record['module'] = best_guess_module

        if not record.get('module'):
            record['module'] = 'unknown'
        elif record['module'] in alias_module_map:
            record['module'] = alias_module_map[record['module']]

    def _process_email(self, record):
        record['primary_key'] = record['message_id']
        record['author_email'] = record['author_email'].lower()

        self._update_record_and_user(record)
        self._guess_module(record)

        if not record.get('blueprint_id'):
            del record['body']

        yield record

    def _process_blueprint(self, record):
        bpd_author = record.get('drafter') or record.get('owner')

        bpd = dict([(k, v) for k, v in six.iteritems(record)
                    if k.find('_link') < 0])
        bpd['record_type'] = 'bpd'
        bpd['primary_key'] = 'bpd:' + record['id']
        bpd['launchpad_id'] = bpd_author
        bpd['date'] = record['date_created']

        self._update_record_and_user(bpd)

        yield bpd

        if record.get('assignee') and record['date_completed']:
            bpc = dict([(k, v) for k, v in six.iteritems(record)
                        if k.find('_link') < 0])
            bpc['record_type'] = 'bpc'
            bpc['primary_key'] = 'bpc:' + record['id']
            bpc['launchpad_id'] = record['assignee']
            bpc['date'] = record['date_completed']

            self._update_record_and_user(bpc)

            yield bpc

    def _process_member(self, record):
        user_id = "member:" + record['member_id']
        record['primary_key'] = user_id
        record['date'] = utils.member_date_to_timestamp(record['date_joined'])
        record['author_name'] = record['member_name']
        record['country'] = record.get("country")
        record['email'] = record.get("email")
        record['module'] = 'unknown'
        company_draft = record['company_draft']

        company_name = self.domains_index.get(utils.normalize_company_name(
            company_draft)) or company_draft

        # author_email is a key to create new user
        record['author_email'] = record["email"] or user_id
        record['company_name'] = company_name
        # _update_record_and_user function will create new user if needed
        self._update_record_and_user(record)
        record['company_name'] = company_name
        user = utils.load_user(self.runtime_storage_inst,
                               record['user_id'] or user_id)
        user['user_name'] = record['author_name']
        user['companies'] = [{
            'company_name': company_name,
            'end_date': 0,
        }]
        user['company_name'] = company_name

        utils.store_user(self.runtime_storage_inst, user)

        record['company_name'] = company_name

        yield record

    def _apply_type_based_processing(self, record):
        if record['record_type'] == 'commit':
            for r in self._process_commit(record):
                yield r
        elif record['record_type'] == 'review':
            for r in self._process_review(record):
                yield r
        elif record['record_type'] == 'email':
            for r in self._process_email(record):
                yield r
        elif record['record_type'] == 'bp':
            for r in self._process_blueprint(record):
                yield r
        elif record['record_type'] == 'member':
            for r in self._process_member(record):
                yield r

    def _renew_record_date(self, record):
        record['week'] = utils.timestamp_to_week(record['date'])
        if ('release' not in record) or (not record['release']):
            record['release'] = self._get_release(record['date'])

    def process(self, record_iterator):
        for record in record_iterator:
            for r in self._apply_type_based_processing(record):

                if r['company_name'] == '*robots':
                    continue

                self._renew_record_date(r)

                yield r

    def _update_records_with_releases(self, release_index):
        LOG.debug('Update records with releases')

        for record in self.runtime_storage_inst.get_all_records():
            if record['primary_key'] in release_index:
                release = release_index[record['primary_key']]
            else:
                release = self._get_release(record['date'])

            if record['release'] != release:
                record['release'] = release
                yield record

    def _update_records_with_user_info(self):
        LOG.debug('Update user info in records')

        for record in self.runtime_storage_inst.get_all_records():
            company_name = record['company_name']
            user_id = record['user_id']
            author_name = record['author_name']

            self._update_record_and_user(record)

            if ((record['company_name'] != company_name) or
                    (record['user_id'] != user_id) or
                    (record['author_name'] != author_name)):
                LOG.debug('User info (%(id)s, %(name)s, %(company)s) has '
                          'changed in record %(record)s',
                          {'id': user_id, 'name': author_name,
                           'company': company_name, 'record': record})
                yield record

    def _update_commits_with_merge_date(self):
        change_id_to_date = {}
        for record in self.runtime_storage_inst.get_all_records():
            if (record['record_type'] == 'review' and
                    record.get('status') == 'MERGED'):
                change_id_to_date[record['id']] = record['lastUpdated']

        for record in self.runtime_storage_inst.get_all_records():
            if record['record_type'] == 'commit':
                change_id_list = record.get('change_id')
                if change_id_list and len(change_id_list) == 1:
                    change_id = change_id_list[0]
                    if change_id in change_id_to_date:
                        old_date = record['date']
                        if old_date != change_id_to_date[change_id]:
                            record['date'] = change_id_to_date[change_id]
                            self._renew_record_date(record)
                            LOG.debug('Date %(date)s has changed in record '
                                      '%(record)s', {'date': old_date,
                                                     'record': record})
                            yield record

    def _update_blueprints_with_mention_info(self):
        LOG.debug('Process blueprints and calculate mention info')

        valid_blueprints = {}
        mentioned_blueprints = {}
        for record in self.runtime_storage_inst.get_all_records():
            for bp in record.get('blueprint_id', []):
                if bp in mentioned_blueprints:
                    mentioned_blueprints[bp]['count'] += 1
                    if record['date'] > mentioned_blueprints[bp]['date']:
                        mentioned_blueprints[bp]['date'] = record['date']
                else:
                    mentioned_blueprints[bp] = {
                        'count': 1,
                        'date': record['date']
                    }
            if record['record_type'] in ['bpd', 'bpc']:
                valid_blueprints[record['id']] = {
                    'primary_key': record['primary_key'],
                    'count': 0,
                    'date': record['date']
                }

        for bp_name, bp in six.iteritems(valid_blueprints):
            if bp_name in mentioned_blueprints:
                bp['count'] = mentioned_blueprints[bp_name]['count']
                bp['date'] = mentioned_blueprints[bp_name]['date']
            else:
                bp['count'] = 0
                bp['date'] = 0

        for record in self.runtime_storage_inst.get_all_records():
            need_update = False

            valid_bp = set([])
            for bp in record.get('blueprint_id', []):
                if bp in valid_blueprints:
                    valid_bp.add(bp)
                else:
                    LOG.debug('Update record %s: removed invalid bp: %s',
                              record['primary_key'], bp)
                    need_update = True
            record['blueprint_id'] = list(valid_bp)

            if record['record_type'] in ['bpd', 'bpc']:
                bp = valid_blueprints[record['id']]
                if ((record.get('mention_count') != bp['count']) or
                        (record.get('mention_date') != bp['date'])):
                    record['mention_count'] = bp['count']
                    record['mention_date'] = bp['date']
                    LOG.debug('Update record %s: mention stats: (%s:%s)',
                              record['primary_key'], bp['count'], bp['date'])
                    need_update = True

            if need_update:
                yield record

    def _update_reviews_with_sequence_number(self):
        LOG.debug('Set review number in review records')

        users_reviews = {}
        for record in self.runtime_storage_inst.get_all_records():
            if record['record_type'] == 'review':
                ldap_id = record['ldap_id']
                review = {'date': record['date'], 'id': record['id']}
                if ldap_id in users_reviews:
                    users_reviews[ldap_id].append(review)
                else:
                    users_reviews[ldap_id] = [review]

        reviews_index = {}
        for ldap_id, reviews in six.iteritems(users_reviews):
            reviews.sort(key=lambda x: x['date'])
            review_number = 0
            for review in reviews:
                review_number += 1
                review['review_number'] = review_number
                reviews_index[review['id']] = review

        for record in self.runtime_storage_inst.get_all_records():
            if record['record_type'] == 'review':
                review = reviews_index[record['id']]
                if record.get('review_number') != review['review_number']:
                    record['review_number'] = review['review_number']
                    yield record

    def _determine_core_contributors(self):
        LOG.debug('Determine core contributors')

        core_engineers = {}
        quarter_ago = int(time.time()) - 60 * 60 * 24 * 30 * 3  # a quarter ago

        for record in self.runtime_storage_inst.get_all_records():
            if (record['record_type'] == 'mark' and
                    record['date'] > quarter_ago and
                    record['value'] in [2, -2]):
                module_branch = (record['module'], record['branch'])
                user_id = record['user_id']
                if user_id in core_engineers:
                    core_engineers[user_id].add(module_branch)
                else:
                    core_engineers[user_id] = set([module_branch])
        for user in self.runtime_storage_inst.get_all_users():
            core_old = user.get('core')
            user['core'] = list(core_engineers.get(user['user_id'], []))
            if user['core'] != core_old:
                utils.store_user(self.runtime_storage_inst, user)

    def _close_patch(self, cores, marks):
        if len(marks) < 2:
            return

        core_mark = 0
        for mark in sorted(marks, key=lambda x: x['date'], reverse=True):

            if core_mark == 0:
                if (mark['module'], mark['branch'], mark['user_id']) in cores:
                    # mark is from core engineer
                    core_mark = mark['value']
                    continue

            disagreement = ((core_mark != 0) and
                            ((core_mark < 0 < mark['value']) or
                             (core_mark > 0 > mark['value'])))
            old_disagreement = mark.get('disagreement', False)
            mark['disagreement'] = disagreement
            if old_disagreement != disagreement:
                yield mark

    def _update_marks_with_disagreement(self):
        LOG.debug('Process marks to find disagreements')

        cores = set()
        for user in self.runtime_storage_inst.get_all_users():
            for (module, branch) in (user['core'] or []):
                cores.add((module, branch, user['user_id']))

        # map from review_id to current patch and list of marks
        marks_per_patch = collections.defaultdict(
            lambda: {'patch_number': 0, 'marks': []})

        for record in self.runtime_storage_inst.get_all_records():
            if (record['record_type'] == 'mark' and
                    record['type'] == 'Code-Review'):
                review_id = record['review_id']
                patch_number = record['patch']

                if review_id in marks_per_patch:
                    # review is already seen, check if patch is newer
                    if (marks_per_patch[review_id]['patch_number'] <
                            patch_number):
                        # the patch is new, close the current
                        for processed in self._close_patch(
                                cores, marks_per_patch[review_id]['marks']):
                            yield processed
                        del marks_per_patch[review_id]

                marks_per_patch[review_id]['patch_number'] = patch_number
                marks_per_patch[review_id]['marks'].append(record)

        # purge the rest
        for marks_patch in marks_per_patch.values():
            for processed in self._close_patch(cores, marks_patch['marks']):
                yield processed

    def update(self, release_index=None):
        self.runtime_storage_inst.set_records(
            self._update_records_with_user_info())

        if release_index:
            self.runtime_storage_inst.set_records(
                self._update_records_with_releases(release_index))

        self.runtime_storage_inst.set_records(
            self._update_reviews_with_sequence_number())

        self.runtime_storage_inst.set_records(
            self._update_blueprints_with_mention_info())

        self.runtime_storage_inst.set_records(
            self._update_commits_with_merge_date())

        self._determine_core_contributors()

        # disagreement calculation must go after determining core contributors
        self.runtime_storage_inst.set_records(
            self._update_marks_with_disagreement())
