import re
from dataclasses import dataclass
from enum import Enum, auto
from urllib.parse import ParseResult, urlparse

import structlog
from django.conf import settings

from readthedocs.builds.constants import EXTERNAL, INTERNAL
from readthedocs.builds.models import Version
from readthedocs.constants import pattern_opts
from readthedocs.projects.models import Domain, Feature, Project

log = structlog.get_logger(__name__)


class UnresolverError(Exception):
    pass


class InvalidXRTDSlugHeaderError(UnresolverError):

    pass


class DomainError(UnresolverError):
    def __init__(self, domain):
        self.domain = domain


class SuspiciousHostnameError(DomainError):
    pass


class InvalidSubdomainError(DomainError):
    pass


class InvalidExternalDomainError(DomainError):
    pass


class InvalidCustomDomainError(DomainError):
    pass


class VersionNotFoundError(UnresolverError):
    def __init__(self, project, version_slug, filename):
        self.project = project
        self.version_slug = version_slug
        self.filename = filename


class TranslationNotFoundError(UnresolverError):
    def __init__(self, project, language, filename):
        self.project = project
        self.language = language
        self.filename = filename


class InvalidPathForVersionedProjectError(UnresolverError):
    def __init__(self, project, path):
        self.project = project
        self.path = path


class InvalidExternalVersionError(UnresolverError):
    def __init__(self, project, version_slug, external_version_slug):
        self.project = project
        self.version_slug = version_slug
        self.external_version_slug = external_version_slug


@dataclass(slots=True)
class UnresolvedURL:

    """Dataclass with the parts of an unresolved URL."""

    # This is the project that owns the domain,
    # this usually is the parent project of a translation or subproject.
    parent_project: Project

    # The current project we are serving the docs from.
    # It can be the same as parent_project.
    project: Project

    version: Version
    filename: str
    parsed_url: ParseResult
    domain: Domain = None
    external: bool = False


class DomainSourceType(Enum):

    """Where the custom domain was resolved from."""

    custom_domain = auto()
    public_domain = auto()
    external_domain = auto()
    http_header = auto()


@dataclass(slots=True)
class UnresolvedDomain:
    # The domain that was used to extract the information from.
    source_domain: str
    source: DomainSourceType
    project: Project
    # Domain object for custom domains.
    domain: Domain = None
    external_version_slug: str = None

    @property
    def is_from_custom_domain(self):
        return self.source == DomainSourceType.custom_domain

    @property
    def is_from_public_domain(self):
        return self.source == DomainSourceType.public_domain

    @property
    def is_from_http_header(self):
        return self.source == DomainSourceType.http_header

    @property
    def is_from_external_domain(self):
        return self.source == DomainSourceType.external_domain


class Unresolver:
    # This pattern matches:
    # - /en
    # - /en/
    # - /en/latest
    # - /en/latest/
    # - /en/latest/file/name/
    multiversion_pattern = re.compile(
        r"""
        ^/(?P<language>{lang_slug})  # Must have the language slug.
        (/((?P<version>{version_slug})(/(?P<file>{filename_slug}))?)?)?$  # Optionally a version followed by a file.  # noqa
        """.format(
            **pattern_opts
        ),
        re.VERBOSE,
    )

    # This pattern matches:
    # - /projects/subproject
    # - /projects/subproject/
    # - /projects/subproject/file/name/
    subproject_pattern = re.compile(
        r"""
        ^/projects/  # Must have the `projects` prefix.
        (?P<project>{project_slug}+)  # Followed by the subproject alias.
        (/(?P<file>{filename_slug}))?$  # Optionally a filename, which will be recursively resolved.
        """.format(
            **pattern_opts
        ),
        re.VERBOSE,
    )

    def unresolve_url(self, url, append_indexhtml=True):
        """
        Turn a URL into the component parts that our views would use to process them.

        This is useful for lots of places,
        like where we want to figure out exactly what file a URL maps to.

        :param url: Full URL to unresolve (including the protocol and domain part).
        :param append_indexhtml: If `True` directories will be normalized
         to end with ``/index.html``.
        """
        parsed_url = urlparse(url)
        domain = self.get_domain_from_host(parsed_url.netloc)
        unresolved_domain = self.unresolve_domain(domain)
        return self._unresolve(
            unresolved_domain=unresolved_domain,
            parsed_url=parsed_url,
            append_indexhtml=append_indexhtml,
        )

    def unresolve_path(self, unresolved_domain, path, append_indexhtml=True):
        """
        Unresolve a path given a unresolved domain.

        This is the same as the unresolve method,
        but this method takes an unresolved domain
        from unresolve_domain as input.

        :param unresolved_domain: An UnresolvedDomain object.
        :param path: Path to unresolve (this shouldn't include the protocol or querystrings).
        :param append_indexhtml: If `True` directories will be normalized
         to end with ``/index.html``.
        """
        # We don't call unparse() on the path,
        # since it could be parsed as a full URL if it starts with a protocol.
        parsed_url = ParseResult(
            scheme="", netloc="", path=path, params="", query="", fragment=""
        )
        return self._unresolve(
            unresolved_domain=unresolved_domain,
            parsed_url=parsed_url,
            append_indexhtml=append_indexhtml,
        )

    def _unresolve(self, unresolved_domain, parsed_url, append_indexhtml):
        """
        The actual unresolver.

        Extracted into a separate method so it can be re-used by
        the unresolve and unresolve_path methods.
        """
        current_project, version, filename = self._unresolve_path_with_parent_project(
            parent_project=unresolved_domain.project,
            path=parsed_url.path,
            external_version_slug=unresolved_domain.external_version_slug,
        )

        if append_indexhtml and filename.endswith("/"):
            filename += "index.html"

        return UnresolvedURL(
            parent_project=unresolved_domain.project,
            project=current_project,
            version=version,
            filename=filename,
            parsed_url=parsed_url,
            domain=unresolved_domain.domain,
            external=unresolved_domain.is_from_external_domain,
        )

    @staticmethod
    def _normalize_filename(filename):
        """Normalize filename to always start with ``/``."""
        filename = filename or "/"
        if not filename.startswith("/"):
            filename = "/" + filename
        return filename

    def _match_multiversion_project(
        self, parent_project, path, external_version_slug=None
    ):
        """
        Try to match a multiversion project.

        An exception is raised if we weren't able to find a matching version or language,
        this exception has the current project (useful for 404 pages).

        :returns: A tuple with the current project, version and file.
         Returns `None` if there isn't a total or partial match.
        """
        match = self.multiversion_pattern.match(path)
        if not match:
            return None

        language = match.group("language")
        version_slug = match.group("version")
        file = self._normalize_filename(match.group("file"))

        if parent_project.language == language:
            project = parent_project
        else:
            project = parent_project.translations.filter(language=language).first()
            if not project:
                raise TranslationNotFoundError(
                    project=parent_project, language=language, filename=file
                )

        if external_version_slug and external_version_slug != version_slug:
            raise InvalidExternalVersionError(
                project=project,
                version_slug=version_slug,
                external_version_slug=external_version_slug,
            )

        manager = EXTERNAL if external_version_slug else INTERNAL
        version = project.versions(manager=manager).filter(slug=version_slug).first()
        if not version:
            raise VersionNotFoundError(
                project=project, version_slug=version_slug, filename=file
            )

        return project, version, file

    def _match_subproject(self, parent_project, path, external_version_slug=None):
        """
        Try to match a subproject.

        If the subproject exists, we try to resolve the rest of the path
        with the subproject as the canonical project.

        :returns: A tuple with the current project, version and file.
         Returns `None` if there isn't a total or partial match.
        """
        match = self.subproject_pattern.match(path)
        if not match:
            return None

        subproject_alias = match.group("project")
        file = self._normalize_filename(match.group("file"))
        project_relationship = (
            parent_project.subprojects.filter(alias=subproject_alias)
            .select_related("child")
            .first()
        )
        if project_relationship:
            # We use the subproject as the new parent project
            # to resolve the rest of the path relative to it.
            subproject = project_relationship.child
            response = self._unresolve_path_with_parent_project(
                parent_project=subproject,
                path=file,
                check_subprojects=False,
                external_version_slug=external_version_slug,
            )
            return response
        return None

    def _match_single_version_project(
        self, parent_project, path, external_version_slug=None
    ):
        """
        Try to match a single version project.

        By default any path will match. If `external_version_slug` is given,
        that version is used instead of the project's default version.

        An exception is raised if we weren't able to find a matching version,
        this exception has the current project (useful for 404 pages).

        :returns: A tuple with the current project, version and file.
         Returns `None` if there isn't a total or partial match.
        """
        file = self._normalize_filename(path)
        if external_version_slug:
            version_slug = external_version_slug
            manager = EXTERNAL
        else:
            version_slug = parent_project.default_version
            manager = INTERNAL

        version = (
            parent_project.versions(manager=manager).filter(slug=version_slug).first()
        )
        if not version:
            raise VersionNotFoundError(
                project=parent_project, version_slug=version_slug, filename=file
            )

        return parent_project, version, file

    def _unresolve_path_with_parent_project(
        self, parent_project, path, check_subprojects=True, external_version_slug=None
    ):
        """
        Unresolve `path` with `parent_project` as base.

        If the returned project is `None`, then we weren't able to
        unresolve the path into a project.

        If the returned version is `None`, then we weren't able to
        unresolve the path into a valid version of the project.

        The checks are done in the following order:

        - Check for multiple versions if the parent project
          isn't a single version project.
        - Check for subprojects.
        - Check for single versions if the parent project isn’t
          a multi version project.

        :param parent_project: The project that owns the path.
        :param path: The path to unresolve.
        :param check_subprojects: If we should check for subprojects,
         this is used to call this function recursively when
         resolving the path from a subproject (we don't support subprojects of subprojects).
        :param external_version_slug: Slug of the external version.
         Used instead of the default version for single version projects
         being served under an external domain.

        :returns: A tuple with: project, version, and file name.
        """
        # Multiversion project.
        if not parent_project.single_version:
            response = self._match_multiversion_project(
                parent_project=parent_project,
                path=path,
                external_version_slug=external_version_slug,
            )
            if response:
                return response

        # Subprojects.
        if check_subprojects:
            response = self._match_subproject(
                parent_project=parent_project,
                path=path,
                external_version_slug=external_version_slug,
            )
            if response:
                return response

        # Single version project.
        if parent_project.single_version:
            response = self._match_single_version_project(
                parent_project=parent_project,
                path=path,
                external_version_slug=external_version_slug,
            )
            if response:
                return response

        raise InvalidPathForVersionedProjectError(
            project=parent_project,
            path=self._normalize_filename(path),
        )

    @staticmethod
    def get_domain_from_host(host):
        """
        Get the normalized domain from a hostname.

        A hostname can include the port.
        """
        return host.lower().split(":")[0]

    def unresolve_domain(self, domain):
        """
        Unresolve domain by extracting relevant information from it.

        :param str domain: Domain to extract the information from.
        :returns: A UnresolvedDomain object.
        """
        public_domain = self.get_domain_from_host(settings.PUBLIC_DOMAIN)
        external_domain = self.get_domain_from_host(
            settings.RTD_EXTERNAL_VERSION_DOMAIN
        )

        subdomain, *root_domain = domain.split(".", maxsplit=1)
        root_domain = root_domain[0] if root_domain else ""

        if public_domain in domain:
            # Serve from the PUBLIC_DOMAIN, ensuring it looks like `foo.PUBLIC_DOMAIN`.
            if public_domain == root_domain:
                project_slug = subdomain
                log.debug("Public domain.", domain=domain)
                return UnresolvedDomain(
                    source_domain=domain,
                    source=DomainSourceType.public_domain,
                    project=self._resolve_project_slug(project_slug, domain),
                )

            # NOTE: This can catch some possibly valid domains (docs.readthedocs.io.com)
            # for example, but these might be phishing, so let's block them for now.
            log.warning("Weird variation of our domain.", domain=domain)
            raise SuspiciousHostnameError(domain=domain)

        # Serve PR builds on external_domain host.
        if external_domain in domain:
            if external_domain == root_domain:
                try:
                    project_slug, version_slug = subdomain.rsplit("--", maxsplit=1)
                    log.debug("External versions domain.", domain=domain)
                    return UnresolvedDomain(
                        source_domain=domain,
                        source=DomainSourceType.external_domain,
                        project=self._resolve_project_slug(project_slug, domain),
                        external_version_slug=version_slug,
                    )
                except ValueError:
                    log.info(
                        "Invalid format of external versions domain.", domain=domain
                    )
                    raise InvalidExternalDomainError(domain=domain)

            # NOTE: This can catch some possibly valid domains (docs.readthedocs.build.com)
            # for example, but these might be phishing, so let's block them for now.
            log.warning("Weird variation of our domain.", domain=domain)
            raise SuspiciousHostnameError(domain=domain)

        # Custom domain.
        domain_object = (
            Domain.objects.filter(domain=domain).select_related("project").first()
        )
        if not domain_object:
            log.info("Invalid domain.", domain=domain)
            raise InvalidCustomDomainError(domain=domain)

        log.debug("Custom domain.", domain=domain)
        return UnresolvedDomain(
            source_domain=domain,
            source=DomainSourceType.custom_domain,
            project=domain_object.project,
            domain=domain_object,
        )

    def _resolve_project_slug(self, slug, domain):
        """Get the project from the slug or raise an exception if not found."""
        try:
            return Project.objects.get(slug=slug)
        except Project.DoesNotExist:
            raise InvalidSubdomainError(domain=domain)

    def unresolve_domain_from_request(self, request):
        """
        Unresolve domain by extracting relevant information from the request.

        We first check if the ``X-RTD-Slug`` header has been set for explicit
        project mapping, otherwise we unresolve by calling `self.unresolve_domain`
        on the host.

        :param request: Request to extract the information from.
        :returns: A UnresolvedDomain object.
        """
        host = self.get_domain_from_host(request.get_host())
        log.bind(host=host)

        # Explicit Project slug being passed in.
        header_project_slug = request.headers.get("X-RTD-Slug", "").lower()
        if header_project_slug:
            project = Project.objects.filter(
                slug=header_project_slug,
                feature__feature_id=Feature.RESOLVE_PROJECT_FROM_HEADER,
            ).first()
            if project:
                log.info(
                    "Setting project based on X_RTD_SLUG header.",
                    project_slug=project.slug,
                )
                return UnresolvedDomain(
                    source_domain=host,
                    source=DomainSourceType.http_header,
                    project=project,
                )
            log.warning(
                "X-RTD-Header passed for project without it enabled.",
                project_slug=header_project_slug,
            )
            raise InvalidXRTDSlugHeaderError

        return unresolver.unresolve_domain(host)


unresolver = Unresolver()
unresolve = unresolver.unresolve_url
