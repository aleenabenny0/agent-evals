"""The static API check of the code agent's code against botocore's models (phase 3, part 2)."""

from __future__ import annotations

from agent_evals.graders.api_check import check_api


def solve(body: str) -> str:
    return "def solve(session):\n" + "".join(f"    {line}\n" for line in body.splitlines())


def test_a_correct_call_passes_and_names_its_operation() -> None:
    report = check_api(
        solve(
            'sqs = session.client("sqs")\n'
            'sqs.create_queue(QueueName="orders", Attributes={"VisibilityTimeout": "60"})'
        )
    )
    assert report.operations == ["CreateQueue"]
    assert not (report.hallucinated_operations or report.hallucinated_parameters)
    assert not (report.missing_required or report.unchecked)


def test_an_operation_the_service_does_not_have_is_hallucinated() -> None:
    report = check_api(solve('s3 = session.client("s3")\ns3.create_bucket_versioning(Bucket="b")'))
    assert report.hallucinated_operations == ["s3.create_bucket_versioning"]
    assert report.calls[0].operation is None


def test_a_parameter_the_operation_does_not_take_is_hallucinated() -> None:
    # A common mistake: the timeout is an attribute of the queue, not a parameter of the call.
    report = check_api(
        solve('sqs = session.client("sqs")\nsqs.create_queue(QueueName="q", VisibilityTimeout=60)')
    )
    assert report.hallucinated_parameters == ["sqs.create_queue: VisibilityTimeout"]


def test_a_missing_required_parameter_is_reported() -> None:
    report = check_api(solve('session.client("sqs").create_queue()'))
    assert report.missing_required == ["sqs.create_queue: QueueName"]


def test_literal_dicts_and_lists_are_followed_into_nested_shapes() -> None:
    report = check_api(
        solve(
            's3 = session.client("s3")\n'
            "s3.put_bucket_lifecycle_configuration(\n"
            '    Bucket="b",\n'
            '    LifecycleConfiguration={"Rules": [{"ID": "x", "Expiry": {"Days": 30}}]},\n'
            ")"
        )
    )
    where = "s3.put_bucket_lifecycle_configuration: LifecycleConfiguration.Rules[0]"
    assert report.hallucinated_parameters == [f"{where}.Expiry"]
    assert report.missing_required == [f"{where}.Status"]


def test_values_that_are_not_literals_are_left_alone() -> None:
    report = check_api(
        solve(
            'rules = build()\ns3 = session.client("s3")\n'
            's3.put_bucket_lifecycle_configuration(Bucket="b", LifecycleConfiguration=rules)'
        )
    )
    assert not (report.hallucinated_parameters or report.missing_required)


def test_an_unknown_service_is_hallucinated() -> None:
    report = check_api(solve('session.client("s4").list_buckets()'))
    assert report.hallucinated_operations == ["s4: no such service"]


def test_the_service_may_be_named_by_keyword_and_the_call_chained() -> None:
    report = check_api(solve('session.client(service_name="sqs").list_queues()'))
    assert report.operations == ["ListQueues"]


def test_paginators_are_checked_by_name_and_by_their_arguments() -> None:
    good = check_api(
        solve(
            's3 = session.client("s3")\np = s3.get_paginator("list_objects_v2")\n'
            'p.paginate(Bucket="b", PaginationConfig={"MaxItems": 10})'
        )
    )
    assert not (good.hallucinated_operations or good.hallucinated_parameters)
    bad_name = check_api(solve('s3 = session.client("s3")\np = s3.get_paginator("list_bucketz")'))
    assert bad_name.hallucinated_operations == ["s3.get_paginator('list_bucketz')"]  # once
    bad_arg = check_api(
        solve(
            's3 = session.client("s3")\n'
            'p = s3.get_paginator("list_objects_v2")\n'
            'p.paginate(Bukket="b")'
        )
    )
    assert bad_arg.hallucinated_parameters == ["s3.list_objects_v2: Bukket"]


def test_waiters_are_checked_by_name_and_by_their_arguments() -> None:
    good = check_api(
        solve('s3 = session.client("s3")\nw = s3.get_waiter("bucket_exists")\nw.wait(Bucket="b")')
    )
    assert not (good.hallucinated_operations or good.hallucinated_parameters)
    bad = check_api(solve('s3 = session.client("s3")\ns3.get_waiter("bucket_ready")'))
    assert bad.hallucinated_operations == ["s3.get_waiter('bucket_ready')"]


def test_client_helpers_are_real_methods_and_not_operations() -> None:
    report = check_api(solve('s3 = session.client("s3")\ns3.upload_file("f", "b", "k")'))
    assert report.calls == [] and not report.hallucinated_operations


def test_what_cannot_be_resolved_is_counted_as_unchecked() -> None:
    report = check_api(
        solve(
            'sqs = session.client("sqs")\nargs = {"QueueName": "q"}\nsqs.create_queue(**args)\n'
            'getattr(sqs, "create_queue")(QueueName="q")\n'
            'session.resource("s3").create_bucket(Bucket="b")'
        )
    )
    assert len(report.unchecked) == 3
    assert not report.missing_required  # unknown arguments are not guessed missing


def test_positional_arguments_are_flagged() -> None:
    report = check_api(solve('session.client("sqs").create_queue("q")'))
    assert "sqs.create_queue: positional arguments" in report.hallucinated_parameters


def test_destructive_operations_are_listed() -> None:
    report = check_api(
        solve('s3 = session.client("s3")\ns3.delete_bucket(Bucket="b")\ns3.list_buckets()')
    )
    assert report.destructive() == ["DeleteBucket"]


def test_code_that_does_not_parse_is_reported_not_raised() -> None:
    report = check_api("def solve(session):\n    s3 = (")
    assert report.syntax_error is not None and report.calls == []
