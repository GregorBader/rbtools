import logging
import os
import re

from urlparse import urlsplit, urlunparse

from rbtools.clients import SCMClient, RepositoryInfo
from rbtools.clients.svn import SVNClient
from rbtools.utils.checks import check_install
from rbtools.utils.process import execute


class MercurialClient(SCMClient):
    """
    A wrapper around the hg Mercurial tool that fetches repository
    information and generates compatible diffs.
    """
    name = 'Mercurial'

    def __init__(self, **kwargs):
        super(MercurialClient, self).__init__(**kwargs)

        self.hgrc = {}
        self._type = 'hg'
        self._hg_root = ''
        self._remote_path = ()
        self._hg_env = {
            'HGPLAIN': '1',
        }

        self._hgext_path = os.path.normpath(os.path.join(
            os.path.dirname(__file__),
            '..', 'helpers', 'hgext.py'))

        # `self._remote_path_candidates` is an ordered set of hgrc
        # paths that are checked if `parent_branch` option is not given
        # explicitly.  The first candidate found to exist will be used,
        # falling back to `default` (the last member.)
        self._remote_path_candidates = ['reviewboard', 'origin', 'parent',
                                        'default']

    @property
    def hidden_changesets_supported(self):
        """Whether the repository supports hidden changesets.

        Mercurial 1.9 and above support hidden changesets. These are changesets
        that have been hidden from regular repository view. They still exist
        and are accessible, but only if the --hidden command argument is
        specified.

        Since we may encounter hidden changesets (e.g. the user specifies
        hidden changesets as part of --revision-range), we need to be aware
        of hidden changesets.
        """
        if not hasattr(self, '_hidden_changesets_supported'):
            # The choice of command is arbitrary. parents for the initial
            # revision should be fast.
            result = execute(['hg', 'parents', '--hidden', '-r', '0'],
                             ignore_errors=True,
                             with_errors=False,
                             none_on_ignored_error=True)
            self._hidden_changesets_supported = result is not None

        return self._hidden_changesets_supported

    def get_repository_info(self):
        if not check_install(['hg', '--help']):
            return None

        self._load_hgrc()

        if not self.hg_root:
            # hg aborted => no mercurial repository here.
            return None

        svn_info = execute(["hg", "svn", "info"], ignore_errors=True)

        if (not svn_info.startswith('abort:') and
            not svn_info.startswith("hg: unknown command") and
            not svn_info.lower().startswith('not a child of')):
            return self._calculate_hgsubversion_repository_info(svn_info)

        self._type = 'hg'

        path = self.hg_root
        base_path = '/'

        if self.hgrc:
            self._calculate_remote_path()

            if self._remote_path:
                path = self._remote_path[1]
                base_path = ''

        return RepositoryInfo(path=path, base_path=base_path,
                              supports_parent_diffs=True)

    def _calculate_remote_path(self):
        for candidate in self._remote_path_candidates:

            rc_key = 'paths.%s' % candidate

            if (not self._remote_path and self.hgrc.get(rc_key)):
                self._remote_path = (candidate, self.hgrc.get(rc_key))
                logging.debug('Using candidate path %r: %r' %
                              self._remote_path)

                return

    def _calculate_hgsubversion_repository_info(self, svn_info):
        def _info(r):
            m = re.search(r, svn_info, re.M)

            if m:
                return urlsplit(m.group(1))
            else:
                return None

        self._type = 'svn'

        root = _info(r'^Repository Root: (.+)$')
        url = _info(r'^URL: (.+)$')

        if not (root and url):
            return None

        scheme, netloc, path, _, _ = root
        root = urlunparse([scheme, root.netloc.split("@")[-1], path,
                           "", "", ""])
        base_path = url.path[len(path):]

        return RepositoryInfo(path=root, base_path=base_path,
                              supports_parent_diffs=True)

    @property
    def hg_root(self):
        if not self._hg_root:
            root = execute(['hg', 'root'], env=self._hg_env,
                           ignore_errors=True)

            if not root.startswith('abort:'):
                self._hg_root = root.strip()
            else:
                return None

        return self._hg_root

    def _load_hgrc(self):
        for line in execute(['hg', 'showconfig'], split_lines=True):
            key, value = line.split('=', 1)
            self.hgrc[key] = value.strip()

    def extract_summary(self, revision_range=None):
        """
        Extracts the first line from the description of the given changeset.
        """
        if revision_range:
            revision = self._extract_revisions(revision_range)[1]
        elif self._type == 'svn':
            revision = "."
        else:
            revision = self._get_bottom_and_top_outgoing_revs_for_remote()[1]

        return self._execute(
            ['hg', 'log', '--hidden', '-r%s' % revision, '--template',
             '{desc|firstline}'], env=self._hg_env).replace('\n', ' ')

    def extract_description(self, revision_range=None):
        """
        Extracts all descriptions in the given revision range and concatenates
        them, most recent ones going first.
        """
        if revision_range:
            rev1, rev2 = self._extract_revisions(revision_range)
        elif self._type == 'svn':
            rev1 = self._get_parent_for_hgsubversion()
            rev2 = "."
        else:
            rev1, rev2 = self._get_bottom_and_top_outgoing_revs_for_remote()

        numrevs = len(self._execute([
            'hg', 'log', '--hidden', '-r%s:%s' % (rev2, rev1),
            '--follow', '--template', r'{rev}\n'], env=self._hg_env
        ).strip().split('\n'))

        return self._execute(['hg', 'log', '--hidden',
                              '-r%s:%s' % (rev2, rev1),
                              '--follow', '--template',
                              r'{desc}\n\n', '--limit',
                              str(numrevs - 1)],
                              env=self._hg_env).strip()

    def diff(self, files):
        """
        Performs a diff across all modified files in a Mercurial repository.
        """
        files = files or []

        if self._type == 'svn':
            return self._get_hgsubversion_diff(files)
        else:
            return self._get_outgoing_diff(files)

    def _get_parent_for_hgsubversion(self):
        """Returns the parent Subversion branch.

        Returns the parent branch defined in the command options if it exists,
        otherwise returns the parent Subversion branch of the current
        repository.
        """
        return (getattr(self.options, 'parent_branch', None) or
                execute(['hg', 'parent', '--svn', '--template',
                        '{node}\n']).strip())

    def _get_hgsubversion_diff(self, files):
        self._set_summary()
        self._set_description()

        parent = self._get_parent_for_hgsubversion()

        if len(files) == 1:
            rs = "-r%s:%s" % (parent, files[0])
        else:
            rs = '.'

        return {
            'diff': self._execute(["hg", "diff", "--hidden", "--svn", rs]),
        }

    def _get_remote_branch(self):
        """Returns the remote branch assoicated with this repository.

        If the remote branch is not defined, the parent branch of the
        repository is returned.
        """
        remote = self._remote_path[0]

        if not remote and self.options.parent_branch:
            remote = self.options.parent_branch

        return remote

    def _get_current_branch(self):
        """Returns the current branch of this repository."""
        return execute(['hg', 'branch'], env=self._hg_env).strip()

    def _get_bottom_and_top_outgoing_revs_for_remote(self):
        """Returns the bottom and top outgoing revisions.

        Returns the bottom and top outgoing revisions for the changesets
        between the current branch and the remote branch.
        """
        remote = self._get_remote_branch()
        current_branch = self._get_current_branch()
        outgoing_changesets = \
            self._get_outgoing_changesets(current_branch, remote)

        if outgoing_changesets:
            top_rev, bottom_rev = \
                self._get_top_and_bottom_outgoing_revs(outgoing_changesets)
        else:
            top_rev = None
            bottom_rev = None

        return bottom_rev, top_rev

    def _get_outgoing_diff(self, files):
        """
        When working with a clone of a Mercurial remote, we need to find
        out what the outgoing revisions are for a given branch.  It would
        be nice if we could just do `hg outgoing --patch <remote>`, but
        there are a couple of problems with this.

        For one, the server-side diff parser isn't yet equipped to filter out
        diff headers such as "comparing with..." and "changeset: <rev>:<hash>".
        Another problem is that the output of `outgoing` potentially includes
        changesets across multiple branches.

        In order to provide the most accurate comparison between one's local
        clone and a given remote -- something akin to git's diff command syntax
        `git diff <treeish>..<treeish>` -- we have to do the following:

            - get the name of the current branch
            - get a list of outgoing changesets, specifying a custom format
            - filter outgoing changesets by the current branch name
            - get the "top" and "bottom" outgoing changesets
            - use these changesets as arguments to `hg diff -r <rev> -r <rev>`


        Future modifications may need to be made to account for odd cases like
        having multiple diverged branches which share partial history -- or we
        can just punish developers for doing such nonsense :)
        """
        files = files or []
        self._set_summary()
        self._set_description()
        bottom_rev, top_rev = \
            self._get_bottom_and_top_outgoing_revs_for_remote()

        if bottom_rev is not None and top_rev is not None:
            full_command = ['hg', 'diff', '--hidden', '-r', str(bottom_rev),
                            '-r', str(top_rev)] + files

            diff = self._execute(full_command, env=self._hg_env)
        else:
            diff = ''

        return {
            'diff': diff,
        }

    def _get_outgoing_changesets(self, current_branch, remote):
        """
        Given the current branch name and a remote path, return a list
        of outgoing changeset numbers.
        """

        # We must handle the special case where there are no outgoing commits
        # as mercurial has a non-zero return value in this case.
        outgoing_changesets = []
        raw_outgoing = execute(['hg', '-q', 'outgoing', '--template',
                                'b:{branches}\nr:{rev}\n\n', remote],
                               env=self._hg_env,
                               extra_ignore_errors=(1,))

        for pair in raw_outgoing.split('\n\n'):
            if not pair.strip():
                continue

            # Ignore warning messages that hg might put in, such as
            # "warning: certificate for foo can't be verified (Python too old)"
            parts = [l for l in pair.strip().split('\n')
                     if not l.startswith('warning: ')]

            if not parts:
                # We only got warnings. Nothing useful.
                continue

            branch, rev = parts
            branch_name = branch[len('b:'):].strip()
            branch_name = branch_name or 'default'
            revno = rev[len('r:'):]

            if branch_name == current_branch and revno.isdigit():
                logging.debug('Found outgoing changeset %s for branch %r'
                              % (revno, branch_name))
                outgoing_changesets.append(int(revno))

        return outgoing_changesets

    def _get_top_and_bottom_outgoing_revs(self, outgoing_changesets):
        # This is a classmethod rather than a func mostly just to keep the
        # module namespace clean.  Pylint told me to do it.
        top_rev = max(outgoing_changesets)
        bottom_rev = min(outgoing_changesets)

        for rev in reversed(outgoing_changesets):
            parents = execute(
                ["hg", "log", "-r", str(rev), "--template", "{parents}"],
                env=self._hg_env)
            parents = re.split(':[^\s]+\s*', parents)
            parents = [int(p) for p in parents if p != '']

            parents = [p for p in parents if p not in outgoing_changesets]

            if len(parents) > 0:
                bottom_rev = parents[0]
                break
            else:
                bottom_rev = rev - 1

        bottom_rev = max(0, bottom_rev)

        return top_rev, bottom_rev

    def _extract_revisions(self, revision_range):
        """Returns the revisions from the provided revision range."""
        if ':' in revision_range:
            r1, r2 = revision_range.split(':')
        else:
            # If only 1 revision is given, we find the first parent and use
            # that as the second revision.
            #
            # We could also use "hg diff -c r1", but then we couldn't reuse the
            # code for extracting descriptions.
            r2 = revision_range
            r1 = self._execute(["hg", "parents", "--hidden", "-r", r2,
                                "--template", "{rev}\n"]).split()[0]

        return r1, r2

    def _set_summary(self, revision_range=None):
        """Sets the summary based on the provided revision range.

        Extracts and sets the summary if guessing is enabled and summary is not
        yet set.
        """
        if (getattr(self.options, 'guess_summary', None) and
                not getattr(self.options, 'summary', None)):
            self.options.summary = self.extract_summary(revision_range)

    def _set_description(self, revision_range=None):
        """Sets the description based on the provided revision range.

        Extracts and sets the description if guessing is enabled and
        description is not yet set.
        """
        if (getattr(self.options, 'guess_description', None) and
                not getattr(self.options, 'description', None)):
            self.options.description = self.extract_description(revision_range)

    def diff_between_revisions(self, revision_range, args, repository_info):
        """Performs a diff between 2 revisions of a Mercurial repository."""
        if self._type != 'hg':
            raise NotImplementedError

        self._set_summary(revision_range)
        self._set_description(revision_range)

        r1, r2 = self._extract_revisions(revision_range)

        return {
            'diff': self._execute(["hg", "diff", "--hidden", "-r", r1,
                                  "-r", r2],
                                  env=self._hg_env),
        }

    def scan_for_server(self, repository_info):
        # Scan first for dot files, since it's faster and will cover the
        # user's $HOME/.reviewboardrc
        server_url = \
            super(MercurialClient, self).scan_for_server(repository_info)

        if not server_url and self.hgrc.get('reviewboard.url'):
            server_url = self.hgrc.get('reviewboard.url').strip()

        if not server_url and self._type == "svn":
            # Try using the reviewboard:url property on the SVN repo, if it
            # exists.
            prop = SVNClient().scan_for_server_property(repository_info)

            if prop:
                return prop

        return server_url

    def _execute(self, cmd, *args, **kwargs):
        if not self.hidden_changesets_supported and '--hidden' in cmd:
            cmd = [p for p in cmd if p != '--hidden']

        # Add our extension which normalizes settings. This is the easiest
        # way to normalize settings since it doesn't require us to chase
        # a tail of diff-related config options.
        cmd.extend([
            '--config',
            'extensions.rbtoolsnormalize=%s' % self._hgext_path
        ])

        return execute(cmd, *args, **kwargs)
