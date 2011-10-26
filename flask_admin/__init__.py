# -*- coding: utf-8 -*-
"""
    flask.ext.admin
    ~~~~~~~~~~~~~~

    Flask-Admin is a Flask extension module that aims to be a
    flexible, customizable web-based interface to your datastore.

    :copyright: (c) 2011 by wilsaj.
    :license: BSD, see LICENSE for more details.
"""
from __future__ import absolute_import

import datetime
from functools import wraps
import inspect
import os
import time
import types

import flask
from flask import flash, render_template, redirect, request, url_for
from flaskext.sqlalchemy import Pagination
import sqlalchemy as sa
from sqlalchemy.orm.exc import NoResultFound
from wtforms import widgets, validators
from wtforms import fields as wtf_fields
from wtforms.ext.sqlalchemy.orm import model_form, converts, ModelConverter
from wtforms.ext.sqlalchemy import fields as sa_fields


def create_admin_blueprint(
    models, db_session, name='admin', model_forms=None, exclude_pks=True,
    list_view_pagination=25, view_decorator=None, **kwargs):
    """
    Returns a blueprint that provides the admin interface views. The
    blueprint that is returned will still need to be registered to
    your flask app. Additional parameters will be passed to the
    blueprint constructor.

    The parameters are:

    `models`
        Either a module or an iterable that contains the SQLAlchemy
        models that will be made available through the admin
        interface.

    `db_session`
        A SQLAlchemy session that has been set up and bound to an
        engine. See the documentation on using Flask with SQLAlchemy
        for more information on how to set that up.

    `name`
        Specify the name for your blueprint. The name of the blueprint
        preceeds the view names of the endpoints, if for example you
        want to refer to the views using :func:`flask.url_for()`. If
        you are using more than one admin blueprint from within the
        same app, it is necessary to set this value to something
        different for each admin module so the admin blueprints will
        have distinct endpoints.

    `model_forms`
        A dict with model names as keys, mapped to WTForm Form objects
        that should be used as forms for creating and editing
        instances of these models.

    `exclude_pks`
        A Boolean value that specifies whether or not to automatically
        exclude fields representing the primary key from Flask-Admin
        rendered forms. The default is True.

    `list_view_pagination`
        The number of model instances that will be shown in the list
        view if there are more than this number of model
        instances.

    `view_decorator`
        A decorator function that will be applied to each admin view
        function.  In particular, you might want to set this to a
        decorator that handles authentication
        (e.g. login_required). See the
        authentication/view_decorator.py for an example of how this
        might be used.
    """

    admin_blueprint = flask.Blueprint(
        name, 'flask.ext.admin',
        static_folder=os.path.join(_get_admin_extension_dir(), 'static'),
        template_folder=os.path.join(_get_admin_extension_dir(), 'templates'),
        **kwargs)

    model_dict = {}

    if not model_forms:
        model_forms = {}

    #XXX: fix base handling so it will work with non-Declarative models
    if type(models) == types.ModuleType:
        model_dict = dict(
            [(k, v) for k, v in models.__dict__.items()
             if isinstance(v, sa.ext.declarative.DeclarativeMeta)
             and k != 'Base'])
    else:
        model_dict = dict(
            [(model.__name__, model)
             for model in models
             if isinstance(model, sa.ext.declarative.DeclarativeMeta)
             and model.__name__ != 'Base'])

    if model_dict:
        admin_blueprint.form_dict = dict(
            [(k, _form_for_model(v, db_session,
                                 exclude_pk=exclude_pks))
             for k, v in model_dict.items()])
        for model, form in model_forms.items():
            if model in admin_blueprint.form_dict:
                admin_blueprint.form_dict[model] = form

    # if no view decorator was assigned, let view_decorator be a dummy
    # decorator that doesn't really do anything
    if not view_decorator:
        def view_decorator(f):
            @wraps(f)
            def wrapper(*args, **kwds):
                return f(*args, **kwds)
            return wrapper

    def create_index_view():
        @view_decorator
        def index():
            """
            Landing page view for admin module
            """
            return render_template(
                'admin/index.html',
                admin_models=sorted(model_dict.keys()))
        return index

    def create_list_view():
        @view_decorator
        def list_view(model_name):
            """
            Lists instances of a given model, so they can be selected for
            editing or deletion.
            """
            if not model_name in model_dict.keys():
                return "%s cannot be accessed through this admin page" % (
                    model_name,)
            model = model_dict[model_name]
            model_instances = db_session.query(model)
            per_page = list_view_pagination
            page = int(request.args.get('page', '1'))
            offset = (page - 1) * per_page
            items = model_instances.limit(per_page).offset(offset).all()
            pagination = Pagination(model_instances, page, per_page,
                                    model_instances.count(), items)
            return render_template(
                'admin/list.html',
                admin_models=sorted(model_dict.keys()),
                _get_pk_value=_get_pk_value,
                model_instances=pagination.items,
                model_name=model_name,
                pagination=pagination)
        return list_view

    def create_edit_view():
        @view_decorator
        def edit(model_name, model_key):
            """
            Edit a particular instance of a model.
            """
            if not model_name in model_dict.keys():
                return "%s cannot be accessed through this admin page" % (
                    model_name,)

            model = model_dict[model_name]
            model_form = admin_blueprint.form_dict[model_name]

            pk = _get_pk_name(model)
            pk_query_dict = {pk: model_key}

            try:
                model_instance = db_session.query(model).filter_by(
                    **pk_query_dict).one()
            except NoResultFound:
                return "%s not found: %s" % (model_name, model_key)

            if request.method == 'GET':
                form = model_form(obj=model_instance)
                has_file_field = filter(lambda field: isinstance(field, wtf_fields.FileField), form)
                return render_template(
                    'admin/edit.html',
                    admin_models=sorted(model_dict.keys()),
                    model_instance=model_instance,
                    model_name=model_name, form=form, has_file_field=has_file_field)

            elif request.method == 'POST':
                form = model_form(request.form, obj=model_instance)
                has_file_field = filter(lambda field: isinstance(field, wtf_fields.FileField), form)
                if form.validate():
                    model_instance = _populate_model_from_form(
                        model_instance, form)
                    db_session.add(model_instance)
                    db_session.commit()
                    flash('%s updated: %s' % (model_name, model_instance),
                          'success')
                    return redirect(
                        url_for('.list_view',
                                model_name=model_name))
                else:
                    flash('There was an error processing your form. '
                          'This %s has not been saved.' % model_name,
                          'error')
                    return render_template(
                        'admin/edit.html',
                        admin_models=sorted(model_dict.keys()),
                        model_instance=model_instance,
                        model_name=model_name, form=form, has_file_field=has_file_field)
        return edit

    def create_add_view():
        @view_decorator
        def add(model_name):
            """
            Create a new instance of a model.
            """
            if not model_name in model_dict.keys():
                return "%s cannot be accessed through this admin page" % (
                    model_name)
            model = model_dict[model_name]
            model_form = admin_blueprint.form_dict[model_name]
            model_instance = model()
            if request.method == 'GET':
                form = model_form()
                return render_template(
                    'admin/add.html',
                    admin_models=sorted(model_dict.keys()),
                    model_name=model_name,
                    form=form)
            elif request.method == 'POST':
                form = model_form(request.form)
                if form.validate():
                    model_instance = _populate_model_from_form(
                        model_instance, form)
                    db_session.add(model_instance)
                    db_session.commit()
                    flash('%s added: %s' % (model_name, model_instance),
                          'success')
                    return redirect(url_for('.list_view',
                                            model_name=model_name))
                else:
                    flash('There was an error processing your form. This '
                          '%s has not been saved.' % model_name, 'error')
                    return render_template(
                        'admin/add.html',
                        admin_models=sorted(model_dict.keys()),
                        model_name=model_name,
                        form=form)
        return add

    def create_delete_view():
        @view_decorator
        def delete(model_name, model_key):
            """
            Delete an instance of a model.
            """
            if not model_name in model_dict.keys():
                return "%s cannot be accessed through this admin page" % (
                    model_name,)
            model = model_dict[model_name]
            pk = _get_pk_name(model)
            pk_query_dict = {pk: model_key}
            try:
                model_instance = db_session.query(model).filter_by(
                    **pk_query_dict).one()
            except NoResultFound:
                return "%s not found: %s" % (model_name, model_key)
            db_session.delete(model_instance)
            db_session.commit()
            flash('%s deleted: %s' % (model_name, model_instance),
                  'success')
            return redirect(url_for(
                '.list_view',
                model_name=model_name))
        return delete

    admin_blueprint.add_url_rule('/', 'index',
                      view_func=create_index_view())
    admin_blueprint.add_url_rule('/list/<model_name>/',
                      'list_view',
                      view_func=create_list_view())
    admin_blueprint.add_url_rule('/edit/<model_name>/<model_key>/',
                      'edit',
                      view_func=create_edit_view(),
                      methods=['GET', 'POST'])
    admin_blueprint.add_url_rule('/delete/<model_name>/<model_key>/',
                      'delete',
                      view_func=create_delete_view())
    admin_blueprint.add_url_rule('/add/<model_name>/',
                      'add',
                      view_func=create_add_view(),
                      methods=['GET', 'POST'])

    return admin_blueprint


def _get_admin_extension_dir():
    """
    Returns the directory path of this admin extension. This is
    necessary for setting the static_folder and templates_folder
    arguments when creating the blueprint.
    """
    return os.path.dirname(inspect.getfile(inspect.currentframe()))